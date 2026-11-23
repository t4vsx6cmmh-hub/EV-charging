"""Elopladning til biler – lokal server med dashboard.

Start:  python3 -m elopladning            (læser config.json)
        python3 -m elopladning --demo     (kører med simulerede data)
"""
import argparse
import json
import logging
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import calc, prices, wallconnector
from .db import DB
from .eloverblik import Eloverblik
from .timeutil import DK, local_dt, local_midnight

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
log = logging.getLogger("elopladning")

DEFAULTS = {
    "wall_connector_host": "",
    "eloverblik_refresh_token": "",
    "metering_point_id": "",
    "price_area": "DK2",
    "supplier_addon_kr_per_kwh": 0.0,
    "vat": 0.25,
    "manual_tariffs": None,
    "poll_seconds": 30,
    "history_days": 90,
    "ev_detect_kw": 5.0,
    "port": 8080,
    "db_path": "elopladning.db",
}


class App:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.db = DB(os.path.join(ROOT, cfg["db_path"]))
        self.vitals = None
        self.vitals_ts = 0
        self.errors = {}
        self.last_sync = {}
        self.elo = None
        if cfg["eloverblik_refresh_token"] and not demo:
            self.elo = Eloverblik(cfg["eloverblik_refresh_token"])

    # ------------------------------------------------------------ helpers --
    def _err(self, key, exc=None):
        if exc is None:
            self.errors.pop(key, None)
        else:
            self.errors[key] = f"{type(exc).__name__}: {exc}"
            log.warning("%s fejlede: %s", key, exc)

    def tariffs(self):
        manual = calc.tariffs_from_config(self.cfg.get("manual_tariffs"))
        return manual or self.db.get_kv("tariffs", [])

    def price_model(self, t0, t1):
        return calc.PriceModel(self.db.spot(t0 - 3600, t1 + 3600), self.tariffs(),
                               float(self.cfg["supplier_addon_kr_per_kwh"]),
                               float(self.cfg["vat"]))

    # ------------------------------------------------------- baggrundsjob --
    def poll_wall_connector(self):
        host = self.cfg["wall_connector_host"]
        if not host:
            return
        try:
            v = wallconnector.record_poll(self.db, host, int(time.time()))
            if v is not None:
                self.vitals, self.vitals_ts = v, time.time()
            self._err("wall_connector")
        except Exception as e:
            self._err("wall_connector", e)

    def sync_spot(self):
        try:
            last = self.db.last_spot_end()
            today = datetime.now(DK).date()
            start = (local_dt(last).date() if last
                     else today - timedelta(days=self.cfg["history_days"]))
            rows = prices.fetch_spot(start - timedelta(days=1), today + timedelta(days=2),
                                     self.cfg["price_area"])
            self.db.upsert_spot(rows)
            self.last_sync["spotpriser"] = int(time.time())
            self._err("spotpriser")
        except Exception as e:
            self._err("spotpriser", e)

    def sync_eloverblik(self):
        if not self.elo:
            return
        try:
            mpid = self.cfg["metering_point_id"] or self.db.get_kv("mpid")
            if not mpid:
                mpid = self.elo.first_consumption_point()
                self.db.set_kv("mpid", mpid)
            today = datetime.now(DK).date()
            last = self.db.last_meter_end()
            start = (local_dt(last).date() - timedelta(days=2) if last
                     else today - timedelta(days=self.cfg["history_days"]))
            self.db.upsert_meter(self.elo.timeseries(mpid, start, today + timedelta(days=1)))
            self.last_sync["eloverblik_forbrug"] = int(time.time())
            if time.time() - self.db.get_kv("tariffs_ts", 0) > 24 * 3600:
                tariffs = self.elo.charges(mpid)
                if tariffs:
                    self.db.set_kv("tariffs", tariffs)
                    self.db.set_kv("tariffs_ts", int(time.time()))
                    self.last_sync["eloverblik_tariffer"] = int(time.time())
            self._err("eloverblik")
        except Exception as e:
            self._err("eloverblik", e)

    def sync_all(self):
        self.sync_spot()
        self.sync_eloverblik()

    def run_background(self):
        def poller():
            while True:
                self.poll_wall_connector()
                time.sleep(self.cfg["poll_seconds"])

        def syncer():
            while True:
                self.sync_all()
                time.sleep(3600)

        threading.Thread(target=poller, daemon=True).start()
        threading.Thread(target=syncer, daemon=True).start()

    # ------------------------------------------------------------ API-data --
    def api_status(self):
        now = int(time.time())
        pm = self.price_model(now, now + 900)
        v = self.vitals if (self.vitals and time.time() - self.vitals_ts < 300) else None
        wc = {"online": v is not None}
        if v:
            pw = wallconnector.power_w(v)
            wc.update({
                "bil_tilsluttet": bool(v.get("vehicle_connected")),
                "lader": bool(v.get("contactor_closed")) and pw > 100,
                "effekt_kw": round(pw / 1000, 2),
                "session_kwh": round(float(v.get("session_energy_wh", 0)) / 1000, 2),
                "session_min": round(float(v.get("session_s", 0)) / 60),
            })
        return {
            "wall_connector": wc,
            "pris_nu": pm.parts(now),
            "tariffer": [{"navn": t["name"], "antal_priser": len(t["prices"])}
                         for t in self.tariffs()],
            "sidste_sync": self.last_sync,
            "fejl": self.errors,
            "demo": self.demo,
            "konfigureret": {
                "wall_connector": bool(self.cfg["wall_connector_host"]) or self.demo,
                "eloverblik": bool(self.elo) or self.demo,
                "tillaeg_kr": self.cfg["supplier_addon_kr_per_kwh"],
            },
        }

    def api_prices(self, hours=4.0):
        now = int(time.time())
        t0 = local_midnight(datetime.now(DK).date())
        t1 = t0 + 3 * 86400
        pm = self.price_model(t0, t1)
        pts = []
        for t in range(t0, t1, 900):
            p = pm.parts(t)
            if p:
                pts.append({"t": t, **{k: round(v, 4) for k, v in p.items()}})
        return {"nu": now, "priser": pts,
                "billigste_blok": calc.cheapest_block(pm, now, t1, hours)}

    def api_summary(self, group="day", count=31):
        today = datetime.now(DK).date()
        if group == "month":
            y, m = today.year, today.month - (count - 1)
            while m <= 0:
                m += 12
                y -= 1
            start = date(y, m, 1)
        else:
            start = today - timedelta(days=count - 1)
        t0, t1 = local_midnight(start), local_midnight(today + timedelta(days=1))
        pm = self.price_model(t0, t1)
        ev_from = self.db.first_ev_ts()
        return calc.summarize(self.db.meter(t0, t1), self.db.ev_intervals(t0, t1), pm,
                              group, ev_logging_from=ev_from,
                              detect_kw=self.cfg["ev_detect_kw"])

    def api_sessions(self, days=60):
        now = int(time.time())
        t0 = now - days * 86400
        pm = self.price_model(t0, now + 86400)
        return list(reversed(calc.sessions(self.db.ev_intervals(t0, now + 900), pm)))


def make_handler(app):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=STATIC, **kw)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/status":
                    return self._json(app.api_status())
                if u.path == "/api/prices":
                    return self._json(app.api_prices(float(q.get("timer", 4))))
                if u.path == "/api/summary":
                    return self._json(app.api_summary(q.get("group", "day"),
                                                      int(q.get("antal", 31))))
                if u.path == "/api/sessions":
                    return self._json(app.api_sessions(int(q.get("dage", 60))))
            except Exception as e:
                traceback.print_exc()
                return self._json({"fejl": str(e)}, 500)
            return super().do_GET()

        def do_POST(self):
            if urlparse(self.path).path == "/api/sync":
                threading.Thread(target=app.sync_all, daemon=True).start()
                return self._json({"ok": True})
            self._json({"fejl": "ukendt"}, 404)

    return Handler


def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Elopladning til biler")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--demo", action="store_true", help="kør med simulerede data")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    if args.port:
        cfg["port"] = args.port
    if args.demo:
        cfg["db_path"] = "demo.db"
    elif not os.path.exists(args.config):
        raise SystemExit("Mangler config.json – kopier config.example.json og udfyld den.")

    app = App(cfg, demo=args.demo)
    if args.demo:
        from . import demo
        demo.seed(app)
        threading.Thread(target=demo.live_loop, args=(app,), daemon=True).start()
    else:
        app.run_background()

    srv = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), make_handler(app))
    log.info("Dashboard kører på http://localhost:%s", cfg["port"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

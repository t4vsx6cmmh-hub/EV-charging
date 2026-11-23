"""Elopladning til biler – lokal server med dashboard.

Start:  python3 -m elopladning                 (læser config.json)
        python3 -m elopladning --demo          (kører med simulerede data)
        python3 -m elopladning --tesla-login   (log ind på Tesla Fleet API én gang)
"""
import argparse
import json
import logging
import os
import ssl
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import calc, export, prices, smart, tesla, wallconnector
from .config import ConfigError, load_config
from .db import DB
from .eloverblik import Eloverblik
from .net import HTTPError
from .notify import Events, make_notifier
from .timeutil import DK, local_dt, local_midnight

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
log = logging.getLogger("elopladning")


class BadRequest(ValueError):
    status = 400


class UnsupportedMedia(ValueError):
    status = 415


class Forbidden(ValueError):
    status = 403


class App:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.db = DB(os.path.join(ROOT, cfg["db_path"]))
        self.vitals = None
        self.vitals_ts = 0
        self.errors = {}
        self.error_since = {}
        self.last_sync = {}
        self.elo = None
        if cfg["eloverblik_refresh_token"] and not demo:
            self.elo = Eloverblik(cfg["eloverblik_refresh_token"])
        self.car = None
        if demo:
            from .demo import DemoCar
            self.car = DemoCar()
        elif tesla.configured(cfg, self.db):
            try:
                self.car = tesla.from_config(cfg, self.db)
            except Exception as e:
                self._err("tesla", e)
        self.notifier = make_notifier(cfg, demo)
        self.smart = smart.SmartCharger(cfg, self.db, self.car, self.price_model)
        self.events = Events(self, self.notifier)

    # ------------------------------------------------------------ helpers --
    def _err(self, key, exc=None):
        if exc is None:
            if key in self.errors:
                log.info("%s virker igen", key)
            self.errors.pop(key, None)
            self.error_since.pop(key, None)
            return
        msg = str(exc) if isinstance(exc, (HTTPError, RuntimeError)) else f"{type(exc).__name__}: {exc}"
        self.errors[key] = msg
        self.error_since.setdefault(key, time.time())
        log.warning("%s fejlede: %s", key, msg)

    def tariffs(self):
        manual = calc.tariffs_from_config(self.cfg.get("manual_tariffs"))
        return manual or self.db.get_kv("tariffs", [])

    def subscriptions_total(self):
        return float(sum(self.cfg["subscriptions_kr_per_month"].values()))

    def price_model(self, t0, t1):
        return calc.PriceModel(self.db.spot(t0 - 3600, t1 + 3600), self.tariffs(),
                               float(self.cfg["supplier_addon_kr_per_kwh"]),
                               float(self.cfg["vat"]))

    def fresh_vitals(self):
        return self.vitals if (self.vitals and time.time() - self.vitals_ts < 300) else None

    # ------------------------------------------------------- baggrundsjob --
    def poll_wall_connector(self):
        host = self.cfg["wall_connector_host"]
        if not host:
            return
        now = int(time.time())
        v = None
        try:
            v = wallconnector.record_poll(self.db, host, now)
            if v is not None:
                self.vitals, self.vitals_ts = v, time.time()
            self._err("wall_connector")
        except Exception as e:
            self._err("wall_connector", e)
        self.after_vitals(v, now)

    def after_vitals(self, v, now):
        """Smart opladning og notifikationer reagerer på ny status fra laderen."""
        for name, fn in (("smart opladning", self.smart.update), ("notifikationer", self.events.on_vitals)):
            try:
                fn(v, now)
            except Exception:
                log.exception("Fejl i %s", name)

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
            return
        try:
            self.events.on_prices(int(time.time()))
        except Exception:
            log.exception("Fejl i prisnotifikation")

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

    def _poll_once(self):
        self.poll_wall_connector()
        self.events.on_errors(time.time())

    @staticmethod
    def _loop(fn, interval):
        while True:
            try:
                fn()
            except Exception:  # baggrundstråden må aldrig dø
                log.exception("Uventet fejl i baggrundsjob")
            time.sleep(interval)

    def run_background(self):
        threading.Thread(target=self._loop, args=(self._poll_once, self.cfg["poll_seconds"]),
                         daemon=True).start()
        threading.Thread(target=self._loop, args=(self.sync_all, 3600), daemon=True).start()

    # ------------------------------------------------------------ API-data --
    def api_status(self):
        now = int(time.time())
        pm = self.price_model(now, now + 900)
        v = self.fresh_vitals()
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
            "smart_fejl": self.smart.error,
            "demo": self.demo,
            "konfigureret": {
                "wall_connector": bool(self.cfg["wall_connector_host"]) or self.demo,
                "eloverblik": bool(self.elo) or self.demo,
                "tillaeg_kr": self.cfg["supplier_addon_kr_per_kwh"],
                "tesla": self.car is not None,
                "notifikationer": self.notifier is not None,
                "abonnement_kr": self.subscriptions_total(),
            },
        }

    def api_prices(self, hours=4.0):
        now = int(time.time())
        today = datetime.now(DK).date()
        t0, t1 = local_midnight(today), local_midnight(today + timedelta(days=2))
        pm = self.price_model(t0, t1)
        pts = []
        for t in range(t0, t1, 900):
            p = pm.parts(t)
            if p:
                pts.append({"t": t, **{k: round(v, 4) for k, v in p.items()}})
        st = self.smart.state(now)
        plan = ([s["t"] for s in st["plan"]["slots"]]
                if st["plan"] and st["indstillinger"]["mode"] == "smart" else [])
        return {"nu": now, "priser": pts, "plan": plan,
                "billigste_blok": calc.cheapest_block(pm, now, t1, hours)}

    def api_summary(self, group="day", count=31):
        if group not in ("day", "month"):
            raise BadRequest("group skal være day eller month")
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
        rows = calc.summarize(self.db.meter(t0, t1), self.db.ev_intervals(t0, t1), pm,
                              group, ev_logging_from=self.db.first_ev_ts(),
                              detect_kw=self.cfg["ev_detect_kw"])
        return calc.add_subscriptions(rows, group, self.subscriptions_total())

    def api_sessions(self, days=60):
        now = int(time.time())
        t0 = now - days * 86400
        pm = self.price_model(t0, now + 86400)
        return list(reversed(calc.sessions(self.db.ev_intervals(t0, now + 900), pm)))

    def api_export(self, q):
        kind = q.get("type", "perioder")
        stamp = datetime.now(DK).strftime("%Y-%m-%d")
        if kind == "perioder":
            group = q.get("group", "month")
            rows = self.api_summary(group, _int(q, "antal", 12 if group == "month" else 31, 1, 400))
            return f"elopladning-{'maaneder' if group == 'month' else 'dage'}-{stamp}.csv", \
                export.periods_csv(rows)
        if kind == "sessioner":
            rows = self.api_sessions(_int(q, "dage", 365, 1, 3650))
            return f"elopladning-sessioner-{stamp}.csv", export.sessions_csv(rows)
        raise BadRequest("type skal være perioder eller sessioner")

    def api_smart_post(self, body):
        try:
            self.smart.set_settings(body)
        except ValueError as e:
            raise BadRequest(str(e)) from None
        self.after_vitals(self.fresh_vitals(), int(time.time()))
        return self.smart.state()

    def api_notify_test(self):
        if not self.notifier:
            raise BadRequest("Notifikationer er ikke sat op (ntfy_topic i config.json)")
        try:
            self.notifier.send("Elopladning til biler", "Testbesked – notifikationer virker.",
                               ["white_check_mark"])
        except Exception as e:
            raise BadRequest(f"Kunne ikke sende: {e}") from None
        return {"ok": True}


def _int(q, key, default, lo, hi):
    try:
        v = int(q.get(key, default))
    except ValueError:
        raise BadRequest(f"{key} skal være et heltal") from None
    if not lo <= v <= hi:
        raise BadRequest(f"{key} skal ligge mellem {lo} og {hi}")
    return v


def _float(q, key, default, lo, hi):
    try:
        v = float(q.get(key, default))
    except ValueError:
        raise BadRequest(f"{key} skal være et tal") from None
    if not lo <= v <= hi:
        raise BadRequest(f"{key} skal ligge mellem {lo} og {hi}")
    return v


def make_handler(app):
    allowed = set(app.cfg.get("allowed_origins") or [])

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                          ".webmanifest": "application/manifest+json",
                          ".woff2": "font/woff2", ".js": "text/javascript"}

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=STATIC, **kw)

        def end_headers(self):
            # Filer hentes altid på ny ved næste besøg (service workeren klarer offline)
            if not self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, body, ctype, code=200, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code=200):
            self._send(json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", code)

        def _body(self):
            # Kræv JSON: så kan en fremmed hjemmeside ikke sende en "simpel" formular
            # til appen fra din browser (CSRF), og Origin skal passe hvis den sendes.
            origin = self.headers.get("Origin")
            if origin and origin not in allowed and urlparse(origin).netloc not in (
                    self.headers.get("Host"), self.headers.get("X-Forwarded-Host")):
                raise Forbidden("Forespørgsel fra en anden side afvist – tilføj adressen "
                                "til allowed_origins i config.json, hvis det er dig")
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise UnsupportedMedia("Content-Type skal være application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10_000:
                raise BadRequest("For stor forespørgsel")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise BadRequest("Ugyldig JSON") from None
            if not isinstance(data, dict):
                raise BadRequest("Forventede et JSON-objekt")
            return data

        def _handle(self, fn):
            try:
                return fn()
            except (BadRequest, UnsupportedMedia, Forbidden) as e:
                return self._json({"fejl": str(e)}, e.status)
            except Exception as e:
                traceback.print_exc()
                return self._json({"fejl": str(e)}, 500)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            routes = {
                "/api/status": lambda: self._json(app.api_status()),
                "/api/prices": lambda: self._json(app.api_prices(_float(q, "timer", 4, 0.25, 24))),
                "/api/summary": lambda: self._json(app.api_summary(
                    q.get("group", "day"), _int(q, "antal", 31, 1, 400))),
                "/api/sessions": lambda: self._json(app.api_sessions(_int(q, "dage", 60, 1, 3650))),
                "/api/smart": lambda: self._json(app.smart.state()),
                "/api/export.csv": lambda: self._csv(app.api_export(q)),
            }
            if u.path in routes:
                return self._handle(routes[u.path])
            if u.path.startswith("/api/"):
                return self._json({"fejl": "ukendt"}, 404)
            return super().do_GET()

        def _csv(self, result):
            name, text = result
            self._send(text.encode("utf-8"), "text/csv; charset=utf-8",
                       extra={"Content-Disposition": f'attachment; filename="{name}"'})

        def do_POST(self):
            path = urlparse(self.path).path

            def sync():
                self._body()
                threading.Thread(target=app.sync_all, daemon=True).start()
                return self._json({"ok": True})

            routes = {
                "/api/sync": sync,
                "/api/smart": lambda: self._json(app.api_smart_post(self._body())),
                "/api/notify-test": lambda: (self._body(), self._json(app.api_notify_test())),
            }
            if path in routes:
                return self._handle(routes[path])
            self._json({"fejl": "ukendt"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Elopladning til biler")
    ap.add_argument("--config", default=os.environ.get("ELOPLADNING_CONFIG",
                                                       os.path.join(ROOT, "config.json")))
    ap.add_argument("--demo", action="store_true", help="kør med simulerede data")
    ap.add_argument("--port", type=int)
    ap.add_argument("--tesla-login", action="store_true",
                    help="log ind på Tesla Fleet API og gem adgangen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        cfg = load_config(None if args.demo and not os.path.exists(args.config) else args.config)
    except ConfigError as e:
        raise SystemExit(f"Fejl i {args.config}:\n{e}")
    if args.port:
        cfg["port"] = args.port
    if args.demo:
        cfg["db_path"] = os.path.join(ROOT, "demo.db")
    elif not os.path.exists(args.config):
        raise SystemExit("Mangler config.json – kopier config.example.json og udfyld den.")

    if args.tesla_login:
        tesla.login_cli(cfg, DB(cfg["db_path"]))
        return

    app = App(cfg, demo=args.demo)
    if args.demo:
        from . import demo
        demo.seed(app)
        threading.Thread(target=demo.live_loop, args=(app,), daemon=True).start()
    else:
        app.run_background()

    try:
        srv = ThreadingHTTPServer((cfg["listen_host"], cfg["port"]), make_handler(app))
    except OSError as e:
        raise SystemExit(f"Kan ikke starte på port {cfg['port']}: {e}. "
                         "Kører appen allerede? Ellers vælg en anden port med --port.")
    scheme = "http"
    if cfg["tls_cert_file"]:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(cfg["tls_cert_file"], cfg["tls_key_file"])
        except (OSError, ssl.SSLError) as e:
            raise SystemExit(f"Kan ikke læse certifikatet: {e}")
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    log.info("Dashboard kører på %s://localhost:%s", scheme, cfg["port"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from elopladning import calc, demo, export, notify
from elopladning.app import App, make_handler
from elopladning.config import DEFAULTS, ConfigError, load_config
from elopladning.timeutil import INTERVAL


class ConfigTest(unittest.TestCase):
    def write(self, d, content):
        p = os.path.join(d, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return p

    def test_defaults_and_relative_db_path(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(self.write(d, {"price_area": "DK1", "_kommentar": "ok"}))
            self.assertEqual(cfg["price_area"], "DK1")
            self.assertEqual(cfg["db_path"], os.path.join(d, "elopladning.db"))

    def test_example_config_is_valid(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.example.json"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(set(example) - {"_kommentar"}, set(DEFAULTS))
        load_config(os.path.join(root, "config.example.json"))

    def test_errors_are_collected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError) as cm:
                load_config(self.write(d, {"price_area": "DK3", "vat": "25%", "port": 80.5,
                                           "wall_connector_host": "http://192.168.1.5",
                                           "manual_tariffs": {"net_hourly": [0.1] * 23}}))
            msg = str(cm.exception)
            for key in ("price_area", "vat", "port", "wall_connector_host", "net_hourly"):
                self.assertIn(key, msg)

    def test_tls_and_origins_validation(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError) as cm:
                load_config(self.write(d, {"tls_cert_file": "cert.pem",
                                           "allowed_origins": ["pi.local/"]}))
            self.assertIn("tls_key_file", str(cm.exception))
            self.assertIn("allowed_origins", str(cm.exception))
            cfg = load_config(self.write(d, {"tls_cert_file": "c.pem", "tls_key_file": "k.pem"}))
            self.assertEqual(cfg["tls_cert_file"], os.path.join(d, "c.pem"))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError) as cm:
                load_config(self.write(d, '{"port": 8080,}'))
            self.assertIn("linje 1", str(cm.exception))


class ExportTest(unittest.TestCase):
    def test_periods_csv_danish_format(self):
        rows = calc.add_subscriptions([{"periode": "2025-02-03", "elbil_kwh": 11.0, "elbil_kr": 17.875,
                                        "hus_kwh": 0.8, "hus_kr": 1.3, "total_kwh": 11.8,
                                        "total_kr": 19.18, "elbil_estimeret": False,
                                        "maaler_data": True, "mangler_pris": False}], "day", 56.0)
        self.assertEqual(rows[0]["abonnement_kr"], 2.0)  # 56 kr / 28 dage i februar
        text = export.periods_csv(rows)
        self.assertTrue(text.startswith("﻿Periode;Elbil kWh;"))
        line = text.splitlines()[1]
        self.assertEqual(line, "2025-02-03;11,000;17,88;0,800;1,30;11,800;19,18;2,00;21,18;nej;ja;nej")

    def test_month_subscription(self):
        rows = calc.add_subscriptions([{"periode": "2025-02", "total_kr": 100.0}], "month", 45.5)
        self.assertEqual(rows[0]["total_inkl_abonnement_kr"], 145.5)


class AppTest(unittest.TestCase):
    """Starter hele appen med demodata og en rigtig HTTP-server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = load_config(None)
        cfg.update(db_path=os.path.join(cls.tmp.name, "demo.db"),
                   subscriptions_kr_per_month={"Net": 31.0})
        cls.app = App(cfg, demo=True)
        demo.seed(cls.app)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, path, body=None, ctype="application/json", headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if data is not None and ctype:
            h["Content-Type"] = ctype
        r = urllib.request.Request(self.base + path, data=data, headers=h,
                                   method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, resp.headers, raw
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8")

    def get(self, path):
        code, _, raw = self.req(path)
        self.assertEqual(code, 200, raw)
        return json.loads(raw)

    def test_dashboard_and_vendor_served(self):
        code, _, raw = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("Smart opladning", raw)
        self.assertEqual(self.req("/vendor/chart.umd.js")[0], 200)

    def test_status_prices_summary_sessions(self):
        st = self.get("/api/status")
        self.assertTrue(st["demo"])
        self.assertEqual(st["konfigureret"]["abonnement_kr"], 31.0)
        pr = self.get("/api/prices?timer=3")
        self.assertGreaterEqual(len(pr["priser"]), 92)  # mindst ét helt døgn (23 timer ved sommertid)
        self.assertIsNotNone(pr["billigste_blok"])
        months = self.get("/api/summary?group=month&antal=3")
        self.assertEqual(len(months), 3)
        self.assertEqual(months[0]["abonnement_kr"], 31.0)
        days = self.get("/api/summary?group=day&antal=7")
        self.assertTrue(all(r["abonnement_kr"] > 0 for r in days))
        self.assertTrue(self.get("/api/sessions?dage=60"))

    def test_insights_and_house_estimate(self):
        ins = self.get("/api/insights")
        cur = ins["denne"]
        self.assertGreater(cur["kwh"], 0)
        self.assertAlmostEqual(cur["km"], cur["kwh"] / 18 * 100, delta=1)
        self.assertAlmostEqual(cur["benzin_kr_pr_km"], 6 * 13.5 / 100)
        self.assertTrue(0 <= cur["billig_andel"] <= 1)
        self.assertEqual(self.get("/api/insights?maaned=2026-01")["forrige"]["maaned"], "2025-12")
        self.assertEqual(self.req("/api/insights?maaned=jan")[0], 400)
        self.assertIsNotNone(self.get("/api/status")["hus_typisk_kw"])
        pr = self.get("/api/prices?dage_tilbage=1")
        self.assertLess(pr["priser"][0]["t"], pr["i_dag"])

    def test_bad_query_params(self):
        for path in ("/api/summary?antal=x", "/api/summary?antal=0", "/api/summary?group=year",
                     "/api/prices?timer=100", "/api/export.csv?type=noget"):
            code, _, raw = self.req(path)
            self.assertEqual(code, 400, path)
            self.assertIn("fejl", json.loads(raw))
        self.assertEqual(self.req("/api/findesikke")[0], 404)

    def test_export_csv(self):
        code, headers, raw = self.req("/api/export.csv?type=perioder&group=month&antal=2")
        self.assertEqual(code, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(len(raw.strip().splitlines()), 3)
        code, _, raw = self.req("/api/export.csv?type=sessioner&dage=30")
        self.assertTrue(raw.lstrip("﻿").startswith("Start;Slut;kWh"))

    def test_app_files_served_for_install(self):
        code, headers, raw = self.req("/manifest.webmanifest")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/manifest+json")
        manifest = json.loads(raw)
        self.assertEqual(manifest["display"], "standalone")
        sizes = {i["sizes"] for i in manifest["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        for icon in manifest["icons"] + [{"src": "icons/apple-touch-icon.png"}]:
            code, headers, _ = self.req("/" + icon["src"])
            self.assertEqual((code, headers["Content-Type"]), (200, "image/png"), icon["src"])
        code, headers, raw = self.req("/sw.js")
        self.assertEqual(code, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertIn("X-Fra-Cache", raw)
        code, _, html = self.req("/")
        self.assertIn('rel="manifest"', html)

    def test_allowed_origin(self):
        app = self.app
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(
            dict(app.cfg, allowed_origins=["https://pi.eksempel.ts.net"]), demo=True)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base, self.base = self.base, f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            ok = self.req("/api/smart", {"mode": "off"}, headers={"Origin": "https://pi.eksempel.ts.net"})
            self.assertEqual(ok[0], 200)
            proxied = self.req("/api/smart", {"mode": "off"},
                               headers={"Origin": "https://andet.ts.net", "X-Forwarded-Host": "andet.ts.net"})
            self.assertEqual(proxied[0], 200)
            self.assertEqual(self.req("/api/smart", {"mode": "off"},
                                      headers={"Origin": "https://ond.example"})[0], 403)
        finally:
            self.base = base
            srv.shutdown()
            srv.server_close()

    def test_post_requires_json_and_same_origin(self):
        self.assertEqual(self.req("/api/smart", {"mode": "off"}, ctype="text/plain")[0], 415)
        code, _, _ = self.req("/api/smart", {"mode": "off"}, headers={"Origin": "http://ond.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.req("/api/smart", {"mode": "turbo"})[0], 400)

    def test_smart_flow_controls_demo_car(self):
        app = self.app
        code, _, raw = self.req("/api/smart", {"mode": "now", "klar_kl": "06:30", "kwh": 20})
        self.assertEqual(code, 200, raw)
        st = json.loads(raw)
        self.assertEqual(st["indstillinger"], {"mode": "now", "klar_kl": "06:30", "kwh": 20.0,
                                                "maal_pct": None})
        app.car.charging = False
        v = {"vehicle_connected": True, "contactor_closed": False, "session_energy_wh": 0}
        app.after_vitals(v, int(time.time()))
        self.assertTrue(app.car.charging, "Lad nu skal starte bilen")
        self.req("/api/smart", {"mode": "smart"})
        st = self.get("/api/smart")
        self.assertEqual(st["indstillinger"]["mode"], "smart")
        self.assertTrue(st["styrer_bilen"])
        self.assertIsNotNone(st["plan"])
        self.assertIsInstance(self.get("/api/prices")["plan"], list)
        self.assertEqual(self.req("/api/notify-test", {})[0], 200)

    def test_session_end_notification(self):
        app = self.app
        sent = app.notifier.sent
        sent.clear()
        now = int(time.time())
        start = now - now % INTERVAL - 4 * INTERVAL
        app.db.add_ev_kwh({start + i * INTERVAL: 2.5 for i in range(4)})
        ev = notify.Events(app, app.notifier)
        charging = {"vehicle_connected": True, "contactor_closed": True,
                    "currentA_a": 16, "voltageA_v": 230}
        ev.on_vitals(charging, start)
        ev.on_vitals(dict(charging, contactor_closed=False), now)
        self.assertTrue(any(t == "Opladning stoppet" for t, _ in sent), sent)

    def test_error_notification_after_two_hours(self):
        app = self.app
        sent = app.notifier.sent
        sent.clear()
        ev = notify.Events(app, app.notifier)
        app._err("eloverblik", RuntimeError("token afvist"))
        app.error_since["eloverblik"] = time.time() - 3 * 3600
        ev.on_errors(time.time())
        ev.on_errors(time.time())
        self.assertEqual([t for t, _ in sent], ["Problem: Eloverblik"])
        app._err("eloverblik")
        ev.on_errors(time.time())
        self.assertEqual(sent[-1][0], "Eloverblik virker igen")


if __name__ == "__main__":
    unittest.main()

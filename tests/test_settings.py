import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from elopladning import eloverblik
from elopladning.app import App, make_handler
from elopladning.config import load_config

from test_core import FakeWallConnector
from test_integrations import FakeServer


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"_kommentar": "min egen note", "port": 8123, "eloverblik_refresh_token": "TOK-9f8e7d"}, f)
        cfg = load_config(self.path)
        self.app = App(cfg, config_path=self.path)
        self.app.autosync = False
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def req(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET",
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def saved(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_get_hides_secrets(self):
        code, d = self.req("/api/settings")
        self.assertEqual(code, 200)
        self.assertEqual(d["vaerdier"]["eloverblik_refresh_token"], {"saet": True})
        self.assertNotIn("TOK-9f8e7d", json.dumps(d))
        self.assertTrue(d["kan_gemme"])
        self.assertEqual({s["id"] for s in d["afsnit"]} >= {"wallconnector", "solceller", "tesla"}, True)

    def test_save_writes_only_changed_and_keeps_rest(self):
        code, d = self.req("/api/settings", {"wall_connector_host": "192.168.1.77", "supplier_addon_kr_per_kwh": "0,048",
                                             "eloverblik_refresh_token": "", "solar_kwp": 6.5,
                                             "solar_orientation": "oest-vest",
                                             "subscriptions_kr_per_month": {"Net": "26,25", "": 5}})
        self.assertEqual(code, 200, d)
        f = self.saved()
        self.assertEqual(f["_kommentar"], "min egen note")
        self.assertEqual(f["port"], 8123)
        self.assertEqual(f["eloverblik_refresh_token"], "TOK-9f8e7d")  # tomt felt = behold
        self.assertEqual(f["wall_connector_host"], "192.168.1.77")
        self.assertEqual(f["supplier_addon_kr_per_kwh"], 0.048)
        self.assertEqual(f["subscriptions_kr_per_month"], {"Net": 26.25})
        self.assertEqual(self.app.cfg["solar_kwp"], 6.5)       # i brug med det samme
        self.assertEqual(d["status"]["solceller"], "klar")

    def test_validation_errors_and_unknown_fields(self):
        code, d = self.req("/api/settings", {"price_area": "DK3"})
        self.assertEqual(code, 400)
        self.assertIn("Prisområde", d["fejl"])
        code, d = self.req("/api/settings", {"port": 80})
        self.assertEqual(code, 400)
        self.assertIn("kan ikke ændres her", d["fejl"])
        code, d = self.req("/api/settings", {"charge_kw": "mange"})
        self.assertEqual(code, 400)
        self.assertEqual(self.saved().get("charge_kw"), None)   # intet skrevet ved fejl

    def test_secret_can_be_cleared_and_notifier_applied(self):
        self.assertIsNone(self.app.notifier)
        code, _ = self.req("/api/settings", {"ntfy_topic": "mit-emne", "eloverblik_refresh_token": None})
        self.assertEqual(code, 200)
        self.assertIsNotNone(self.app.notifier)
        self.assertIsNone(self.app.elo)
        self.assertEqual(self.saved()["eloverblik_refresh_token"], "")

    def test_creates_config_on_first_start(self):
        os.unlink(self.path)
        self.app.cfg["eloverblik_refresh_token"] = ""
        code, d = self.req("/api/settings")
        self.assertTrue(d["foerste_start"])
        code, d = self.req("/api/settings", {"wall_connector_host": "10.0.0.5"})
        self.assertEqual(code, 200)
        self.assertEqual(self.saved(), {"wall_connector_host": "10.0.0.5"})
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_wall_connector_test_button(self):
        wc = HTTPServer(("127.0.0.1", 0), FakeWallConnector)
        threading.Thread(target=wc.serve_forever, daemon=True).start()
        try:
            code, d = self.req("/api/test/wall-connector", {"wall_connector_host": f"127.0.0.1:{wc.server_address[1]}"})
            self.assertTrue(d["ok"], d)
            self.assertIn("bil tilsluttet", d["tekst"])
            code, d = self.req("/api/test/wall-connector", {"wall_connector_host": "127.0.0.1:1"})
            self.assertFalse(d["ok"])
            self.assertIn("tjek IP-adressen", d["tekst"])
        finally:
            wc.shutdown()
            wc.server_close()

    def test_eloverblik_test_lists_metering_points(self):
        s = FakeServer()
        old = eloverblik.BASE
        try:
            eloverblik.BASE = s.url
            s.routes[("GET", "/token")] = (200, {"result": "adgang"})
            s.routes[("GET", "/meteringpoints/meteringpoints")] = (200, {"result": [
                {"meteringPointId": "571", "typeOfMP": "E17", "streetName": "Solvej", "buildingNumber": "3",
                 "postcode": "8000", "cityName": "Aarhus"},
                {"meteringPointId": "572", "typeOfMP": "E18"}]})
            code, d = self.req("/api/test/eloverblik", {})
            self.assertTrue(d["ok"], d)
            self.assertEqual([p["id"] for p in d["maalepunkter"]], ["571", "572"])
            self.assertIn("solceller", d["tekst"])
        finally:
            eloverblik.BASE = old
            s.close()

    def test_tesla_web_login(self):
        code, d = self.req("/api/tesla/login", {})
        self.assertEqual(code, 400)
        self.req("/api/settings", {"tesla_client_id": "cid", "tesla_client_secret": "sec",
                                   "tesla_redirect_uri": "https://pi.example/tesla/callback"})
        code, d = self.req("/api/tesla/login", {})
        self.assertEqual(code, 200)
        state = parse_qs(urlsplit(d["url"]).query)["state"][0]
        s = FakeServer()
        try:
            s.routes[("POST", "/token")] = (200, {"access_token": "a", "refresh_token": "r1", "expires_in": 60})
            with self.assertRaises(Exception):
                self.app.tesla_finish("https://pi.example/tesla/callback?code=x&state=forkert",
                                      token_url=s.url + "/token")
            st = self.app.tesla_finish(f"https://pi.example/tesla/callback?code=abc&state={state}",
                                       token_url=s.url + "/token")
            self.assertTrue(st["logget_ind"])
            self.assertIsNotNone(self.app.car)
            self.assertIs(self.app.smart.car, self.app.car)
        finally:
            s.close()

    def test_demo_cannot_save(self):
        demo = App(dict(self.app.cfg), demo=True)
        from elopladning.app import BadRequest
        with self.assertRaises(BadRequest):
            demo.api_settings_post({"wall_connector_host": "1.2.3.4"})


if __name__ == "__main__":
    unittest.main()

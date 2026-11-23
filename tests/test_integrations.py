import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from elopladning import net, notify, tesla
from elopladning.db import DB


class FakeServer:
    """Lille HTTP-server, hvor hver test bestemmer svarene via `routes`."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                key = (self.command, self.path.split("?")[0])
                resp = outer.routes.get(key, (404, {}))
                if callable(resp):
                    resp = resp(body)
                if isinstance(resp, list):  # svar efter tur
                    resp = resp.pop(0) if len(resp) > 1 else resp[0]
                code, data = resp
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _do

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class NetTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()

    def tearDown(self):
        self.s.close()

    def test_retries_on_503_then_succeeds(self):
        self.s.routes[("GET", "/x")] = [(503, {}), (503, {}), (200, {"ok": 1})]
        waits = []
        self.assertEqual(net.request_json(self.s.url + "/x", sleep=waits.append), {"ok": 1})
        self.assertEqual(waits, [2.0, 4.0])

    def test_no_retry_on_404_and_readable_error(self):
        with self.assertRaises(net.HTTPError) as cm:
            net.request_json(self.s.url + "/mangler", sleep=lambda s: None)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(self.s.requests), 1)
        self.assertIn("HTTP 404", str(cm.exception))

    def test_gives_up_after_retries(self):
        self.s.routes[("GET", "/x")] = [(500, {})]
        with self.assertRaises(net.HTTPError):
            net.request_json(self.s.url + "/x", retries=1, sleep=lambda s: None)
        self.assertEqual(len(self.s.requests), 2)


class TeslaTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.n_tokens = 0

        def token(body):
            self.n_tokens += 1
            q = parse_qs(body)
            if q["refresh_token"][0] not in ("seed", f"rt{self.n_tokens - 1}"):
                return (401, {"error": "invalid_grant"})
            return (200, {"access_token": f"at{self.n_tokens}", "refresh_token": f"rt{self.n_tokens}",
                          "expires_in": 28800})

        vin = "/api/1/vehicles/VIN1"
        self.s.routes.update({
            ("POST", "/token"): token,
            ("GET", "/api/1/vehicles"): (200, {"response": [{"vin": "VIN1"}]}),
            ("GET", vin + "/vehicle_data"): (200, {"response": {"charge_state": {
                "battery_level": 55, "charge_limit_soc": 80}}}),
            ("POST", vin + "/command/charge_start"): (200, {"response": {"result": True, "reason": ""}}),
            ("POST", vin + "/command/charge_stop"): (200, {"response": {"result": False, "reason": "not_charging"}}),
        })

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def car(self, token="seed"):
        return tesla.TeslaFleet("cid", token, self.db, api_base=self.s.url,
                                token_url=self.s.url + "/token")

    def test_charge_state_and_commands(self):
        car = self.car()
        self.assertEqual(car.charge_state()["battery_level"], 55)
        self.assertEqual(car.vin, "VIN1")
        self.assertEqual(car.charge_start(), "")
        self.assertEqual(car.charge_stop(), "not_charging")  # allerede stoppet = ok
        auth = [r[2].get("Authorization") for r in self.s.requests if "/api/1/" in r[1]]
        self.assertTrue(all(a == "Bearer at1" for a in auth))

    def test_rotated_refresh_token_is_persisted(self):
        self.car().access_token()
        self.assertEqual(self.db.get_kv("tesla_tokens"), {"seed": "seed", "refresh": "rt1"})
        # Efter genstart bruges det gemte (roterede) token, ikke det gamle fra config
        self.car().access_token()
        self.assertEqual(self.db.get_kv("tesla_tokens")["refresh"], "rt2")

    def test_rejected_command_and_asleep(self):
        self.s.routes[("POST", "/api/1/vehicles/VIN1/command/charge_start")] = (
            200, {"response": {"result": False, "reason": "cable_not_connected"}})
        car = self.car()
        with self.assertRaises(tesla.TeslaError):
            car.charge_start()
        self.s.routes[("GET", "/api/1/vehicles/VIN1/vehicle_data")] = (408, {})
        with self.assertRaises(tesla.VehicleAsleep):
            car.charge_state()

    def test_expired_access_token_is_renewed(self):
        car = self.car()
        car.access_token()
        self.s.routes[("GET", "/api/1/vehicles/VIN1/vehicle_data")] = [
            (401, {}), (200, {"response": {"charge_state": {"battery_level": 60, "charge_limit_soc": 80}}})]
        self.assertEqual(car.charge_state()["battery_level"], 60)
        self.assertEqual(self.n_tokens, 2)

    def test_login_cli(self):
        self.s.routes[("POST", "/token")] = (200, {"access_token": "a", "refresh_token": "fresh",
                                                   "expires_in": 100})
        cfg = {"tesla_client_id": "cid", "tesla_client_secret": "sec",
               "tesla_redirect_uri": "https://example.com/cb", "tesla_refresh_token": "",
               "tesla_api_base": self.s.url, "tesla_region": "eu", "tesla_vin": "",
               "tesla_command_proxy": "", "tesla_proxy_ca_file": ""}
        out = []

        def fake_input(prompt):
            url = next(line for line in out if "authorize" in line).strip()
            state = parse_qs(url.split("?", 1)[1])["state"][0]
            return f"https://example.com/cb?code=abc&state={state}"

        tesla.login_cli(cfg, self.db, input_fn=fake_input, print_fn=out.append,
                        token_url=self.s.url + "/token")
        self.assertEqual(self.db.get_kv("tesla_tokens")["refresh"], "fresh")
        sent = parse_qs(self.s.requests[0][3])
        self.assertEqual(sent["code"], ["abc"])
        self.assertEqual(sent["grant_type"], ["authorization_code"])
        self.assertTrue(tesla.configured(cfg, self.db))


class NtfyTest(unittest.TestCase):
    def test_send_json_with_danish_letters(self):
        s = FakeServer()
        try:
            s.routes[("POST", "/")] = (200, {"id": "x"})
            notify.Ntfy("emne", s.url, token="tok").send("Opladning stoppet", "32,4 kWh · 41,20 kr",
                                                          ["electric_plug"])
            method, path, headers, body = s.requests[0]
            data = json.loads(body)
            self.assertEqual(data["topic"], "emne")
            self.assertEqual(data["message"], "32,4 kWh · 41,20 kr")
            self.assertEqual(headers["Authorization"], "Bearer tok")
        finally:
            s.close()

    def test_dk_format(self):
        self.assertEqual(notify.dk(1234.5), "1.234,50")
        self.assertEqual(notify.dk(3.14159, 1), "3,1")


if __name__ == "__main__":
    unittest.main()

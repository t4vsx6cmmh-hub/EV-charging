import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from elopladning import calc, eloverblik, prices, wallconnector
from elopladning.db import DB
from elopladning.timeutil import parse_iso_utc


class FakeWallConnector(BaseHTTPRequestHandler):
    energy_wh = 1_000_000.0

    def do_GET(self):
        if self.path == "/api/1/lifetime":
            body = {"energy_wh": FakeWallConnector.energy_wh}
        elif self.path == "/api/1/vitals":
            body = {"vehicle_connected": True, "contactor_closed": True,
                    "currentA_a": 16, "currentB_a": 16, "currentC_a": 16,
                    "voltageA_v": 230, "voltageB_v": 230, "voltageC_v": 230,
                    "session_energy_wh": 5000, "session_s": 1800}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class WallConnectorTest(unittest.TestCase):
    def test_spread(self):
        b = wallconnector.spread_energy(900 * 10 + 450, 900 * 11 + 450, 2.0)
        self.assertEqual(b, {9000: 1.0, 9900: 1.0})

    def test_poll_records_intervals(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeWallConnector)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{srv.server_address[1]}"
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, "t.db"))
            t = 1_700_000_100
            wallconnector.record_poll(db, host, t)
            FakeWallConnector.energy_wh += 2750  # 2,75 kWh på 15 min = 11 kW
            v = wallconnector.record_poll(db, host, t + 900)
            rows = db.ev_intervals(0, 2_000_000_000)
            self.assertAlmostEqual(sum(r["kwh"] for r in rows), 2.75)
            self.assertAlmostEqual(wallconnector.power_w(v), 3 * 16 * 230)
        srv.shutdown()


class ParseTest(unittest.TestCase):
    def test_eloverblik_timeseries(self):
        res = {"result": [{"MyEnergyData_MarketDocument": {"TimeSeries": [{"Period": [{
            "resolution": "PT15M",
            "timeInterval": {"start": "2025-11-01T23:00:00Z", "end": "2025-11-02T23:00:00Z"},
            "Point": [{"position": "1", "out_Quantity.quantity": "0.25", "out_Quantity.quality": "A04"},
                      {"position": "2", "out_Quantity.quantity": "2.9", "out_Quantity.quality": "A04"}]}]}]}}]}
        rows = eloverblik.parse_timeseries(res)
        t0 = parse_iso_utc("2025-11-01T23:00:00Z")
        self.assertEqual(rows, [(t0, 900, 0.25), (t0 + 900, 900, 2.9)])

    def test_eloverblik_charges(self):
        res = {"result": [{"result": {"tariffs": [
            {"name": "Nettarif C time", "owner": "Radius", "periodType": "PT1H",
             "prices": [{"position": str(i + 1), "price": 0.1 * (i // 6 + 1)} for i in range(24)]},
            {"name": "Elafgift", "owner": "Energinet", "periodType": "P1D",
             "prices": [{"position": "1", "price": 0.008}]}]}}]}
        t = eloverblik.parse_charges(res)
        self.assertEqual(len(t), 2)
        self.assertEqual(len(t[0]["prices"]), 24)

    def test_dayahead(self):
        rows = prices.parse_dayahead([{"TimeUTC": "2025-11-01T10:15:00", "PriceArea": "DK2",
                                       "DayAheadPriceDKK": 812.5}])
        self.assertEqual(rows, [(parse_iso_utc("2025-11-01T10:15:00Z"), 900, 812.5)])


class CalcTest(unittest.TestCase):
    def setUp(self):
        self.t0 = parse_iso_utc("2025-11-01T23:00:00Z")  # midnat dansk tid 2. nov
        spot = [(self.t0 + i * 900, 900, 1000.0 if i < 8 else 2000.0) for i in range(96)]
        tariffs = [{"name": "flad", "prices": [0.2]}]
        self.pm = calc.PriceModel(spot, tariffs, addon=0.1, vat=0.25)

    def test_price(self):
        # (1.0 spot + 0.1 tillæg + 0.2 tarif) * 1.25
        self.assertAlmostEqual(self.pm.price(self.t0), 1.625)

    def test_split(self):
        meter = [{"start": self.t0 + i * 900, "seconds": 900, "kwh": 0.1 + (2.75 if i < 4 else 0)}
                 for i in range(8)]
        ev = [{"start": self.t0 + i * 900, "kwh": 2.75} for i in range(4)]
        out = calc.summarize(meter, ev, self.pm, "day", ev_logging_from=self.t0 - 1)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertAlmostEqual(r["elbil_kwh"], 11.0)
        self.assertAlmostEqual(r["hus_kwh"], 0.8)
        self.assertAlmostEqual(r["elbil_kr"], round(11 * 1.625, 2))

    def test_estimate_without_wall_connector(self):
        meter = [{"start": self.t0 + i * 900, "seconds": 900,
                  "kwh": 0.1 + (2.75 if 2 <= i < 6 else 0)} for i in range(16)]
        out = calc.summarize(meter, [], self.pm, "day", ev_logging_from=None)
        self.assertTrue(out[0]["elbil_estimeret"])
        self.assertAlmostEqual(out[0]["elbil_kwh"], 11.0, places=2)

    def test_sessions_and_cheapest(self):
        ev = [{"start": self.t0 + (8 + i) * 900, "kwh": 2.75} for i in range(4)]  # dyre kvarterer
        s = calc.sessions(ev, self.pm)
        self.assertEqual(len(s), 1)
        self.assertGreaterEqual(s[0]["mulig_besparelse_kr"], 0)
        blk = calc.cheapest_block(self.pm, self.t0, self.t0 + 86400, 2)
        self.assertEqual(blk["start"], self.t0)


if __name__ == "__main__":
    unittest.main()

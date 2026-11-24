import unittest
from urllib.parse import parse_qs

from elopladning import calc, smart, solar
from elopladning.timeutil import parse_iso_utc

from test_integrations import FakeServer

T0 = parse_iso_utc("2025-06-01T22:00:00Z")  # midnat dansk sommertid 2. juni


def pm_flat(price_kr=1.0, days=2):
    # spot 1 kr/kWh hele døgnet; ingen tariffer/moms -> købspris = salgsværdi + gebyr
    return calc.PriceModel([(T0 + i * 900, 900, price_kr * 1000) for i in range(96 * days)], [], 0.0, 0.0)


class ForecastTest(unittest.TestCase):
    def test_irradiance_to_kw(self):
        self.assertEqual(solar.kw_from_irradiance(0, 20, 6, 0.85), 0.0)
        self.assertAlmostEqual(solar.kw_from_irradiance(800, 0, 6, 0.85), 6 * 0.8 * 0.85)
        hot = solar.kw_from_irradiance(800, 30, 6, 0.85)
        self.assertLess(hot, 6 * 0.8 * 0.85)  # varme paneler giver lidt mindre

    def test_parse_uses_preceding_hour(self):
        res = {"hourly": {"time": [T0 + 3600, T0 + 7200], "global_tilted_irradiance": [0, 500],
                          "temperature_2m": [10, 10]}}
        rows = solar.parse_forecast(res, 4.0, 1.0)
        self.assertEqual(rows[1][0], T0 + 3600)          # værdien kl. 02 dækker 01–02
        self.assertGreater(rows[1][2], 1.9)

    def test_fetch_east_west_adds_both_planes(self):
        s = FakeServer()
        try:
            def answer(body):
                return (200, {"hourly": {"time": [T0 + 3600], "global_tilted_irradiance": [400],
                                         "temperature_2m": [5]}})
            s.routes[("GET", "/v1/forecast")] = answer
            cfg = {"solar_kwp": 8.0, "solar_efficiency": 1.0, "solar_orientation": "oest-vest",
                   "solar_tilt": 30, "solar_lat": None, "solar_lon": None, "price_area": "DK1"}
            rows = solar.fetch_forecast(cfg, base_url=s.url + "/v1/forecast")
            self.assertEqual(len(s.requests), 2)
            azimuths = sorted(parse_qs(r[1].split("?", 1)[1])["azimuth"][0] for r in s.requests)
            self.assertEqual(azimuths, ["-90", "90"])
            q = parse_qs(s.requests[0][1].split("?", 1)[1])
            self.assertEqual((q["latitude"][0], q["longitude"][0]), ("56.16", "10.2"))
            self.assertAlmostEqual(rows[0][2], 2 * 4.0 * 0.4, places=2)  # to planer à 4 kWp
        finally:
            s.close()


class PricingTest(unittest.TestCase):
    def test_charge_price_blends_solar_surplus(self):
        fc = solar.SolarForecast([(T0 + 12 * 3600, 3600, 5.9)])  # 5,9 kW kl. 12-13
        cp = solar.ChargePrices(pm_flat(2.0), fc, charge_kw=11.0, base_kw=0.4, fee=0.1)
        noon = T0 + 12 * 3600
        self.assertAlmostEqual(cp.solar_share(noon), 0.5)
        self.assertAlmostEqual(cp.price(noon), 0.5 * (2.0 - 0.1) + 0.5 * 2.0)
        self.assertEqual(cp.price(T0), 2.0)  # ingen sol om natten

    def test_plan_prefers_sunny_hours(self):
        # dyr strøm hele dagen, men et stort anlæg giver fuldt overskud kl. 11-14
        fc = solar.SolarForecast([(T0 + h * 3600, 3600, 15.0) for h in (11, 12, 13)])
        cp = solar.ChargePrices(pm_flat(2.0), fc, 11.0, 0.4, fee=1.5)  # salgsværdi 0,50 kr
        plan = smart.make_plan(cp, T0 + 8 * 3600, T0 + 18 * 3600, 22.0, 11.0)
        hours = sorted({(s["t"] - T0) // 3600 for s in plan["slots"]})
        self.assertEqual(hours, [11, 12])
        self.assertAlmostEqual(plan["kr"], 22 * 0.5)
        self.assertAlmostEqual(plan["sol_kwh"], 22.0)

    def test_ev_accounting_counts_solar_at_sell_value(self):
        # bilen ladede 2,75 kWh i et kvarter, hvor måleren kun købte 1,0 kWh fra nettet
        meter = [{"start": T0, "seconds": 900, "kwh": 1.0}]
        ev = [{"start": T0, "kwh": 2.75}]
        evp = solar.EvPrices(pm_flat(2.0), meter, ev, fee=0.1)
        self.assertAlmostEqual(evp.solar_kwh(T0), 1.75)
        out = calc.summarize(meter, ev, evp, "day", ev_logging_from=T0 - 1)[0]
        self.assertAlmostEqual(out["elbil_kwh"], 2.75)           # ikke skaleret ned
        self.assertAlmostEqual(out["elbil_sol_kwh"], 1.75)
        self.assertAlmostEqual(out["elbil_kr"], round(1.0 * 2.0 + 1.75 * 1.9, 2))
        self.assertAlmostEqual(out["hus_kwh"], 0.0)
        ins = calc.insights(ev, evp, T0, T0 + 86400, T0 + 86400,
                            {"consumption_kwh_per_100km": 18, "petrol_l_per_100km": 6,
                             "petrol_price_kr_per_l": 13})
        self.assertAlmostEqual(ins["sol_kwh"], 1.8, places=1)

    def test_without_solar_old_scaling_is_kept(self):
        meter = [{"start": T0, "seconds": 900, "kwh": 1.0}]
        ev = [{"start": T0, "kwh": 2.75}]
        out = calc.summarize(meter, ev, pm_flat(2.0), "day", ev_logging_from=T0 - 1)[0]
        self.assertAlmostEqual(out["elbil_kwh"], 1.0)


if __name__ == "__main__":
    unittest.main()

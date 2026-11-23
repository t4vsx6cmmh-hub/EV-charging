import unittest
from datetime import date

from elopladning import calc, smart
from elopladning.tesla import VehicleAsleep
from elopladning.timeutil import local_dt, local_midnight, parse_iso_utc

T0 = parse_iso_utc("2025-11-01T23:00:00Z")  # midnat dansk tid 2. nov


def price_model(n_days=2, cheap=range(8, 16)):
    """Spot 2 kr/kWh, men 0,5 kr i kvartererne `cheap` (hver dag). Ingen tariffer/moms."""
    spot = [(T0 + i * 900, 900, 500.0 if (i % 96) in cheap else 2000.0)
            for i in range(96 * n_days)]
    return calc.PriceModel(spot, [], addon=0.0, vat=0.0)


class FakeCar:
    def __init__(self, level=50, limit=80):
        self.calls = []
        self.level, self.limit = level, limit
        self.asleep = False

    def charge_state(self):
        self.calls.append("state")
        if self.asleep:
            raise VehicleAsleep("zzz")
        return {"battery_level": self.level, "charge_limit_soc": self.limit}

    def charge_start(self):
        self.calls.append("start")
        if self.asleep:
            raise VehicleAsleep("zzz")
        return "complete" if self.level >= self.limit else ""

    def charge_stop(self):
        self.calls.append("stop")
        return ""

    def wake(self):
        self.calls.append("wake")
        self.asleep = False


class Store(dict):
    def get_kv(self, k, default=None):
        return self.get(k, default)

    def set_kv(self, k, v):
        self[k] = v


CFG = {"smart_default_ready": "07:00", "smart_default_kwh": 30, "charge_kw": 11.0,
       "battery_kwh": 75.0, "charge_efficiency": 0.9}


def vitals(connected=True, charging=False, session_wh=0):
    amps = 16 if charging else 0
    return {"vehicle_connected": connected, "contactor_closed": charging,
            "currentA_a": amps, "currentB_a": amps, "currentC_a": amps,
            "voltageA_v": 230, "voltageB_v": 230, "voltageC_v": 230,
            "session_energy_wh": session_wh}


class PlanTest(unittest.TestCase):
    def test_picks_cheapest_slots(self):
        plan = smart.make_plan(price_model(), T0, T0 + 6 * 3600, 11.0, 11.0)  # 4 kvarterer
        self.assertEqual([s["t"] for s in plan["slots"]], [T0 + i * 900 for i in range(8, 12)])
        self.assertEqual(plan["perioder"], [[T0 + 8 * 900, T0 + 12 * 900]])
        self.assertAlmostEqual(plan["kr"], 11 * 0.5)
        self.assertAlmostEqual(plan["kr_hvis_nu"], 11 * 2.0)
        self.assertFalse(plan["naar_ikke"])

    def test_partial_last_slot_and_not_enough_time(self):
        plan = smart.make_plan(price_model(), T0, T0 + 3600, 20.0, 11.0)
        self.assertTrue(plan["naar_ikke"])
        self.assertEqual(len(plan["slots"]), 4)
        plan = smart.make_plan(price_model(), T0, T0 + 6 * 3600, 4.0, 11.0)
        self.assertEqual(len(plan["slots"]), 2)
        self.assertAlmostEqual(plan["kr"], 4 * 0.5)

    def test_unknown_prices_estimated_from_day_before(self):
        pm = price_model(n_days=1)
        start = T0 + 20 * 3600
        plan = smart.make_plan(pm, start, start + 12 * 3600, 11.0, 11.0)
        self.assertTrue(plan["estimeret"])
        # kvarter 8-11 næste dag skønnes billige ud fra dagen før
        self.assertEqual(plan["slots"][0]["t"], T0 + 86400 + 8 * 900)

    def test_nothing_to_charge(self):
        plan = smart.make_plan(price_model(), T0, T0 + 3600, 0, 11.0)
        self.assertTrue(plan["faerdig"])
        self.assertEqual(plan["slots"], [])


class DeadlineTest(unittest.TestCase):
    def test_next_morning(self):
        now = parse_iso_utc("2025-11-02T20:00:00Z")  # 21:00 dansk tid
        d = local_dt(smart.next_deadline(now, "07:00"))
        self.assertEqual((d.day, d.hour, d.minute), (3, 7, 0))

    def test_same_day(self):
        now = parse_iso_utc("2025-11-02T04:00:00Z")  # 05:00 dansk tid
        d = local_dt(smart.next_deadline(now, "7:30"))
        self.assertEqual((d.day, d.hour, d.minute), (2, 7, 30))

    def test_across_dst_change(self):
        # Natten til 26. okt. 2025 går uret tilbage: 07:00 er da 06:00 UTC (ikke 05:00)
        now = parse_iso_utc("2025-10-25T20:00:00Z")
        self.assertEqual(smart.next_deadline(now, "07:00"), parse_iso_utc("2025-10-26T06:00:00Z"))
        now = parse_iso_utc("2026-03-28T20:00:00Z")  # sommertid fra 29. marts
        self.assertEqual(smart.next_deadline(now, "07:00"), parse_iso_utc("2026-03-29T05:00:00Z"))

    def test_bad_time(self):
        for bad in ("24:00", "7", "ab:cd", ""):
            with self.assertRaises(ValueError):
                smart.parse_hhmm(bad)

    def test_dst_day_lengths(self):
        self.assertEqual(local_midnight(date(2025, 10, 27)) - local_midnight(date(2025, 10, 26)), 25 * 3600)
        self.assertEqual(local_midnight(date(2026, 3, 30)) - local_midnight(date(2026, 3, 29)), 23 * 3600)


class ControllerTest(unittest.TestCase):
    def make(self, car=None, mode="smart", kwh=11):
        store = Store(smart_settings={"mode": mode, "klar_kl": "06:00", "kwh": kwh})
        return smart.SmartCharger(CFG, store, car, lambda a, b: price_model()), store

    def test_starts_in_cheap_slot_and_pauses_otherwise(self):
        car = FakeCar(level=70, limit=80)  # 10 % af 75 kWh / 0,9 = 8,3 kWh -> 4 kvarterer
        sc, _ = self.make(car)
        sc.update(vitals(), T0 + 60)               # dyrt kvarter: ingen grund til at gøre noget
        self.assertEqual(sc.source, "soc")
        self.assertFalse(sc.desired)
        self.assertNotIn("start", car.calls)
        sc.update(vitals(), T0 + 8 * 900 + 10)     # billigt kvarter
        self.assertTrue(sc.desired)
        self.assertIn("start", car.calls)
        car.level = 80                              # bilen har nået ladegrænsen
        sc.update(vitals(charging=True, session_wh=8400), T0 + 12 * 900 + 10)
        self.assertEqual(sc.remaining, 0)
        self.assertEqual(car.calls[-1], "stop")

    def test_rate_limits_commands(self):
        car = FakeCar(level=0, limit=100)
        sc, _ = self.make(car, mode="now")
        for i in range(10):
            sc.update(vitals(), T0 + i * 30)       # bilen reagerer ikke
        self.assertEqual(car.calls.count("start"), 1)
        sc.update(vitals(), T0 + 700)
        self.assertEqual(car.calls.count("start"), 2)

    def test_wakes_sleeping_car(self):
        car = FakeCar(level=0, limit=100)
        car.asleep = True
        sc, _ = self.make(car, mode="now")
        sc.update(vitals(), T0)
        self.assertEqual(car.calls[-1], "wake")
        sc.update(vitals(), T0 + 120)
        self.assertEqual(car.calls[-1], "start")

    def test_full_car_stops_asking(self):
        car = FakeCar(level=80, limit=80)
        sc, _ = self.make(car, mode="now")
        sc.update(vitals(), T0)
        sc.update(vitals(), T0 + 3600)
        self.assertEqual(car.calls.count("start"), 1)
        self.assertTrue(sc.car_full)

    def test_kwh_target_without_car(self):
        sc, _ = self.make(None, kwh=11)
        sc.update(vitals(session_wh=5500), T0)
        self.assertEqual(sc.source, "kwh")
        self.assertAlmostEqual(sc.remaining, 5.5)
        self.assertEqual(len(sc.plan["slots"]), 2)
        st = sc.state(T0)
        self.assertFalse(st["styrer_bilen"])

    def test_off_mode_never_commands(self):
        car = FakeCar(level=0, limit=100)
        sc, _ = self.make(car, mode="off")
        sc.update(vitals(charging=True), T0)
        self.assertNotIn("start", car.calls)
        self.assertNotIn("stop", car.calls)

    def test_own_target_pct_and_level_estimate(self):
        car = FakeCar(level=50, limit=90)
        sc, _ = self.make(car)
        sc.set_settings({"maal_pct": 70})
        sc.update(vitals(session_wh=0), T0)
        # 20 % af 75 kWh / 0,9 = 16,7 kWh
        self.assertAlmostEqual(sc.remaining, 20 / 100 * 75 / 0.9, places=2)
        sc.update(vitals(session_wh=7500), T0 + 600)
        self.assertAlmostEqual(sc.level_now(), 50 + 7.5 * 0.9 / 75 * 100, places=1)
        sc.set_settings({"maal_pct": 100})      # højere end bilens ladegrænse -> 90 %
        self.assertEqual(sc.target_pct(), 90)
        sc.set_settings({"maal_pct": None})
        self.assertEqual(sc.target_pct(), 90)

    def test_unplug_resets_and_preview(self):
        sc, _ = self.make(FakeCar())
        sc.update(vitals(), T0)
        self.assertTrue(sc.plugged)
        sc.update(vitals(connected=False), T0 + 60)
        self.assertFalse(sc.plugged)
        st = sc.state(T0 + 60)
        self.assertTrue(st["forhaandsvisning"])
        self.assertTrue(st["plan"]["slots"])

    def test_settings_validation(self):
        sc, store = self.make()
        sc.set_settings({"mode": "now", "klar_kl": "6:05", "kwh": "20"})
        self.assertEqual(store["smart_settings"], {"mode": "now", "klar_kl": "06:05", "kwh": 20.0,
                                                   "maal_pct": None})
        for bad in ({"mode": "turbo"}, {"kwh": 0}, {"kwh": "x"}, {"klar_kl": "99:00"},
                    {"maal_pct": 10}, {"maal_pct": "fuld"}):
            with self.assertRaises(ValueError):
                sc.set_settings(bad)


if __name__ == "__main__":
    unittest.main()

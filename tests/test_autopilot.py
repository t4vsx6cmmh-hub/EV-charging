import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from elopladning import autopilot, calc, smart
from elopladning.db import DB
from elopladning.timeutil import DK

from test_smart import CFG, FakeCar, Store, vitals


def ts(d, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=DK).timestamp())


MONDAY = date(2025, 11, 3)


def history(weeks=4, weekday="07:20", weekend="10:00", jitter=(0, 10, -10, 5)):
    ev = []
    for k in range(weeks * 7):
        d = MONDAY + timedelta(days=k)
        base = weekday if d.weekday() < 5 else weekend
        t = ts(d, base) + jitter[k % len(jitter)] * 60
        ev.append({"ts": t, "kind": "out", "kwh": 20.0})
        ev.append({"ts": t + 10 * 3600, "kind": "in", "kwh": None})
    return ev


class LearnTest(unittest.TestCase):
    def test_learns_weekday_and_weekend(self):
        now = ts(MONDAY + timedelta(days=28), "20:00")
        m = autopilot.learn(history(), now)
        wd, we = m["dage"][2], m["dage"][6]
        self.assertEqual(wd["typisk"], "07:22")       # median af 07:10–07:30
        self.assertEqual(wd["klar_kl"], "07:00")      # 20 %-kvantil 07:16 minus 15 min, rundet ned
        self.assertEqual(we["kilde"], "søndag")
        self.assertLess(we["klar_kl"], we["typisk"])
        self.assertEqual(m["kwh"], 20)

    def test_falls_back_to_group_then_none(self):
        now = ts(MONDAY + timedelta(days=28), "20:00")
        ev = [e for e in history() if datetime.fromtimestamp(e["ts"], DK).weekday() != 2]
        m = autopilot.learn(ev, now)
        self.assertEqual(m["dage"][2]["kilde"], "hverdage")
        self.assertIsNone(autopilot.learn([], now)["dage"][0])

    def test_ignores_evening_and_old_departures(self):
        now = ts(MONDAY + timedelta(days=200), "20:00")
        self.assertEqual(autopilot.learn(history(), now)["antal_afgange"], 0)
        evening = [{"ts": ts(MONDAY + timedelta(days=k), "18:30"), "kind": "out", "kwh": 5} for k in range(10)]
        self.assertEqual(autopilot.learn(evening, ts(MONDAY + timedelta(days=12), "20:00"))["antal_afgange"], 0)

    def test_next_departure_today_or_tomorrow(self):
        now = ts(MONDAY + timedelta(days=28), "05:00")  # mandag kl. 5: dagens afgang er endnu ikke sket
        m = autopilot.learn(history(), now)
        t, hhmm, info, day = autopilot.next_departure(m, now, "07:00")
        self.assertEqual(day, MONDAY + timedelta(days=28))
        later = ts(MONDAY + timedelta(days=28), "19:00")
        t, hhmm, info, day = autopilot.next_departure(m, later, "07:00")
        self.assertEqual(day, MONDAY + timedelta(days=29))
        self.assertIn("i morgen", autopilot.explain(info, day, MONDAY + timedelta(days=28)).lower())


class AutopilotChargerTest(unittest.TestCase):
    def setUp(self):
        self.now = ts(MONDAY + timedelta(days=28), "20:00")  # mandag aften
        spot = [(ts(MONDAY + timedelta(days=28), "00:00") + i * 900, 900, 1000.0) for i in range(96 * 3)]
        self.pm = calc.PriceModel(spot, [], 0.0, 0.0)
        self.store = Store(smart_settings={"mode": "smart", "klar_kl": "09:00", "kwh": 11,
                                           "maal_pct": None, "autopilot": True})
        self.sc = smart.SmartCharger(CFG, self.store, None, lambda a, b: self.pm,
                                     clock=lambda: self.now, learner=lambda n: autopilot.learn(history(), n))

    def test_deadline_from_learned_habit(self):
        eff = self.sc.effective(self.now)
        self.assertEqual(eff["kilde"], "autopilot")
        self.assertEqual(eff["klar_kl"], "07:00")
        self.assertEqual(eff["deadline"], ts(MONDAY + timedelta(days=29), "07:00"))
        self.assertEqual(eff["kwh"], 20.0)  # lært typisk opladning

    def test_manual_when_autopilot_off(self):
        self.sc.set_settings({"autopilot": False})
        eff = self.sc.effective(self.now)
        self.assertEqual((eff["kilde"], eff["klar_kl"]), ("manuel", "09:00"))

    def test_one_off_override_for_next_departure_only(self):
        self.sc.set_settings({"engang": {"maal_pct": 100, "klar_kl": "05:30"}})
        eff = self.sc.effective(self.now)
        self.assertEqual((eff["kilde"], eff["maal_pct"]), ("engang", 100.0))
        self.assertEqual(eff["deadline"], ts(MONDAY + timedelta(days=29), "05:30"))
        # Dagen efter gælder vanerne igen, og ændringen ryddes op
        after = ts(MONDAY + timedelta(days=30), "20:00")
        self.assertEqual(self.sc.effective(after)["kilde"], "autopilot")
        self.assertIsNone(self.store.get("smart_override"))
        self.sc.set_settings({"engang": None})

    def test_plug_events_are_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, "t.db"))
            sc = smart.SmartCharger(CFG, db, FakeCar(), lambda a, b: self.pm)
            sc.update(vitals(), self.now)
            sc.update(vitals(session_wh=12300), self.now + 600)
            sc.update(vitals(connected=False), self.now + 3600)
            ev = db.plug_events(0)
            self.assertEqual([e["kind"] for e in ev], ["in", "out"])
            self.assertAlmostEqual(ev[1]["kwh"], 12.3)


if __name__ == "__main__":
    unittest.main()

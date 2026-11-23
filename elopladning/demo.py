"""Simulerede data, så dashboardet kan prøves uden Wall Connector og Eloverblik."""
import math
import random
import time
from datetime import datetime, timedelta

from .timeutil import DK, INTERVAL, local_dt, local_midnight

# Eksempeltariffer (kr/kWh ekskl. moms) – kun til demo, ikke dine rigtige priser
DEMO_TARIFFS = [
    {"name": "Nettarif (demo)", "prices": [0.12] * 6 + [0.36] * 11 + [0.94] * 4 + [0.36] * 3},
    {"name": "Energinet tarif (demo)", "prices": [0.14]},
    {"name": "Elafgift (demo)", "prices": [0.01]},
]


def _spot(ts, rnd):
    h = local_dt(ts).hour + local_dt(ts).minute / 60
    base = 650 + 450 * math.exp(-((h - 8) ** 2) / 3) + 700 * math.exp(-((h - 18.5) ** 2) / 4)
    base -= 300 * math.exp(-((h - 3) ** 2) / 6)
    return max(base * rnd + random.gauss(0, 40), -50)


def seed(app):
    random.seed(42)
    db = app.db
    db.execute("DELETE FROM spot")
    db.execute("DELETE FROM meter")
    db.execute("DELETE FROM ev_interval")
    db.set_kv("tariffs", DEMO_TARIFFS)
    today = datetime.now(DK).date()
    start_day = today - timedelta(days=75)
    t0 = local_midnight(start_day)
    t_end = local_midnight(today + timedelta(days=2))
    meter_end = local_midnight(today - timedelta(days=1))  # måler halter ~1 døgn
    now = int(time.time())

    spot_rows, meter_rows, ev = [], [], {}
    day_factor = {}
    for t in range(t0, t_end, INTERVAL):
        d = local_dt(t).date()
        day_factor.setdefault(d, random.uniform(0.6, 1.5))
        spot_rows.append((t, INTERVAL, _spot(t, day_factor[d])))

    # Ladesessioner: hver 2.-3. dag, oftest om natten, nogle gange lige efter arbejde
    d = start_day
    while d <= today:
        if d == today:
            break
        evening = random.random() < 0.3
        hour = random.choice([17, 17.5, 18]) if evening else random.choice([22, 23, 0.5, 1])
        st = local_midnight(d) + int(hour * 3600)
        kwh_needed = random.uniform(15, 45)
        t = st - st % INTERVAL
        while kwh_needed > 0:
            take = min(11 / 4, kwh_needed)
            ev[t] = ev.get(t, 0) + take
            kwh_needed -= take
            t += INTERVAL
        d += timedelta(days=random.choice([2, 2, 3, 4]))

    for t in range(t0, min(meter_end, now), INTERVAL):
        h = local_dt(t).hour
        house_kw = 0.35 + (0.8 if 6 <= h < 8 else 0) + (1.6 if 17 <= h < 20 else 0) \
            + (0.4 if 20 <= h < 23 else 0) + random.uniform(0, 0.5)
        meter_rows.append((t, INTERVAL, house_kw / 4 + ev.get(t, 0) * random.uniform(1.0, 1.03)))

    db.upsert_spot(spot_rows)
    db.upsert_meter(meter_rows)
    db.add_ev_kwh({t: k for t, k in ev.items() if t < now})
    app.last_sync = {"spotpriser": now, "eloverblik_forbrug": now}


def live_loop(app):
    """Simulér at bilen lader nu."""
    session_wh, started = 0.0, time.time()
    while True:
        p = 11000 + random.uniform(-200, 200)
        session_wh += p * 5 / 3600
        app.vitals = {
            "vehicle_connected": True, "contactor_closed": True,
            "currentA_a": p / 3 / 230, "currentB_a": p / 3 / 230, "currentC_a": p / 3 / 230,
            "voltageA_v": 230, "voltageB_v": 230, "voltageC_v": 230,
            "session_energy_wh": session_wh + 6200, "session_s": time.time() - started + 2040,
        }
        app.vitals_ts = time.time()
        time.sleep(5)

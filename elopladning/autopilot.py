"""Autopilot: lær hvornår bilen plejer at køre, så smart opladning selv kan
sætte "klar kl." for hver ugedag.

Grundlaget er Wall Connectorens egne hændelser: hver gang bilen tages ud af
laderen, gemmes tidspunktet. Første afgang om formiddagen (kl. 4–15) på en dag
regnes som "afgangen". For hver ugedag bruges de seneste 8 uger; er der for få
afgange på netop den ugedag, bruges hverdage/weekend samlet.

Klar-tiden sættes forsigtigt: så bilen er klar før 80 % af de kendte afgange,
minus 15 minutters buffer.
"""
import statistics
from datetime import datetime, timedelta

from .timeutil import DK, local_dt

WEEKS = 8
MIN_SAMPLES = 3
BUFFER_MIN = 15
DAY_NAMES = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


def _quantile(values, q):
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _hhmm(minutes):
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def departures(events, now, weeks=WEEKS):
    """Første afgang (minutter efter midnat) pr. dato i perioden."""
    first = {}
    for e in events:
        if e["kind"] != "out" or e["ts"] < now - weeks * 7 * 86400 or e["ts"] > now:
            continue
        d = local_dt(e["ts"])
        if not 4 <= d.hour < 15:
            continue
        m = d.hour * 60 + d.minute
        first[d.date()] = min(first.get(d.date(), m), m)
    return first


def learn(events, now, weeks=WEEKS):
    """Returnér en model: {"dage": [7 x (dict|None)], "kwh": typisk kWh pr. opladning|None,
    "antal_afgange": int}."""
    first = departures(events, now, weeks)
    by_wd = {}
    for day, m in first.items():
        by_wd.setdefault(day.weekday(), []).append(m)

    days = []
    for wd in range(7):
        samples, source = by_wd.get(wd, []), DAY_NAMES[wd]
        if len(samples) < MIN_SAMPLES:
            group = range(5) if wd < 5 else (5, 6)
            samples = [m for g in group for m in by_wd.get(g, [])]
            source = "hverdage" if wd < 5 else "weekender"
        if len(samples) < MIN_SAMPLES:
            days.append(None)
            continue
        ready = (_quantile(samples, 0.2) - BUFFER_MIN) // 15 * 15
        days.append({
            "klar_kl": _hhmm(ready),
            "typisk": _hhmm(round(statistics.median(samples))),
            "fra": _hhmm(round(_quantile(samples, 0.1))),
            "til": _hhmm(round(_quantile(samples, 0.9))),
            "antal": len(samples),
            "kilde": source,
        })

    energies = [e["kwh"] for e in events
                if e["kind"] == "out" and e.get("kwh") and e["kwh"] >= 1
                and now - weeks * 7 * 86400 <= e["ts"] <= now]
    kwh = round(statistics.median(energies)) if len(energies) >= MIN_SAMPLES else None
    return {"dage": days, "kwh": kwh, "antal_afgange": len(first)}


def next_departure(model, now, fallback_hhmm):
    """Næste afgang efter `now`: (unix-tid, klar_kl, ugedagsdata|None, dato)."""
    today = local_dt(now).date()
    for k in range(0, 8):
        day = today + timedelta(days=k)
        info = model["dage"][day.weekday()] if model else None
        hhmm = info["klar_kl"] if info else fallback_hhmm
        h, m = (int(x) for x in hhmm.split(":"))
        t = int(datetime(day.year, day.month, day.day, h, m, tzinfo=DK).timestamp())
        if t > now:
            return t, hhmm, info, day
    raise RuntimeError("Ingen afgang fundet")  # pragma: no cover


def explain(info, day, today):
    """Kort dansk forklaring til dashboardet."""
    when = "i dag" if day == today else "i morgen" if day == today + timedelta(days=1) \
        else DAY_NAMES[day.weekday()]
    if not info:
        return f"{when.capitalize()}: standardtid (autopiloten lærer stadig dine vaner)"
    return (f"{when.capitalize()}: du kører typisk kl. {info['typisk'].replace(':', '.')} "
            f"(lært ud fra {info['antal']} afgange på {info['kilde']})")

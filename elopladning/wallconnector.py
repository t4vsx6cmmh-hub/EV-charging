"""Læser Tesla Wall Connector (Gen 3) via dens lokale API på hjemmenetværket.

Endpoints (ingen login):
  http://<ip>/api/1/vitals    – live: bil tilsluttet, strøm, spænding, sessionens energi
  http://<ip>/api/1/lifetime  – samlet tæller: energy_wh
"""
from .net import request_json
from .timeutil import INTERVAL


def _get(host, path, timeout=5):
    # Ingen genforsøg: appen spørger alligevel igen om lidt
    return request_json(f"http://{host}{path}", timeout=timeout, retries=0)


def read_vitals(host):
    return _get(host, "/api/1/vitals")


def read_lifetime_wh(host):
    return float(_get(host, "/api/1/lifetime")["energy_wh"])


def power_w(vitals):
    """Beregn aktuel ladeeffekt i watt ud fra fasestrømme og -spændinger."""
    total = 0.0
    found = False
    for ph in "abc":
        i = vitals.get(f"current{ph}_a")
        if i is None:
            i = vitals.get(f"current{ph.upper()}_a")
        u = vitals.get(f"voltage{ph}_v")
        if u is None:
            u = vitals.get(f"voltage{ph.upper()}_v")
        if i is not None and u is not None:
            found = True
            total += float(i) * float(u)
    if not found:
        total = float(vitals.get("vehicle_current_a", 0)) * float(vitals.get("grid_v", 230))
    return max(total, 0.0)


def spread_energy(t_prev, t_now, kwh, interval=INTERVAL):
    """Fordel energi forbrugt mellem to aflæsninger ligeligt over tid
    på de 15-minutters intervaller, tidsrummet dækker."""
    if kwh <= 0 or t_now <= t_prev:
        return {}
    span = t_now - t_prev
    out = {}
    t = t_prev
    while t < t_now:
        b = t - (t % interval)
        seg_end = min(b + interval, t_now)
        out[b] = out.get(b, 0.0) + kwh * (seg_end - t) / span
        t = seg_end
    return out


def record_poll(db, host, now):
    """Aflæs lifetime-tælleren og fordel tilvæksten på intervaller.
    Returnerer vitals (eller None ved fejl)."""
    lifetime = read_lifetime_wh(host)
    last = db.last_ev_reading()
    if last is not None:
        delta_kwh = (lifetime - last["lifetime_wh"]) / 1000.0
        # Negativ (nulstillet tæller) eller urealistisk stor tilvækst ignoreres
        max_kwh = 23.0 * (now - last["ts"]) / 3600.0 + 0.5
        if 0 < delta_kwh <= max_kwh:
            db.add_ev_kwh(spread_energy(last["ts"], now, delta_kwh))
    db.add_ev_reading(now, lifetime)
    try:
        return read_vitals(host)
    except Exception:
        return None

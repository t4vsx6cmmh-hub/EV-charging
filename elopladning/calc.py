"""Beregning af kWh-pris pr. kvarter og fordeling af forbrug/omkostning
mellem elbil og resten af husstanden."""
import calendar
import statistics
from collections import OrderedDict

from .timeutil import INTERVAL, local_dt


# ---------------------------------------------------------------- priser ----

def tariffs_from_config(manual):
    """Manuelle tariffer fra config -> samme format som Eloverblik-tariffer."""
    out = []
    if not manual:
        return out
    if manual.get("net_hourly"):
        out.append({"name": "Nettarif (manuel)", "prices": [float(x) for x in manual["net_hourly"]]})
    for name in ("elafgift", "systemtarif", "nettarif_energinet", "flat"):
        if manual.get(name) is not None:
            out.append({"name": name, "prices": [float(manual[name])]})
    return out


def tariff_at(tariffs, ts):
    """Sum af tariffer (kr/kWh ekskl. moms) på tidspunktet ts (dansk lokaltid)."""
    dt = local_dt(ts)
    minute = dt.hour * 60 + dt.minute
    total = 0.0
    for t in tariffs:
        p = t["prices"]
        if not p:
            continue
        slot = min(minute * len(p) // 1440, len(p) - 1)
        total += p[slot]
    return total


class PriceModel:
    def __init__(self, spot_rows, tariffs, addon, vat):
        # spot fordelt på 15-min intervaller
        self.spot = {}
        for r in spot_rows:
            start, sec, dkk_mwh = ((r["start"], r["seconds"], r["dkk_per_mwh"])
                                   if isinstance(r, dict) else r)
            for t in range(start, start + sec, INTERVAL):
                self.spot[t] = dkk_mwh / 1000.0
        self.tariffs = tariffs
        self.addon = addon
        self.vat = vat

    def parts(self, ts):
        b = ts - ts % INTERVAL
        if b not in self.spot:
            return None
        spot = self.spot[b]
        tariff = tariff_at(self.tariffs, b)
        excl = spot + self.addon + tariff
        return {"spot": spot, "tillaeg": self.addon, "tariffer": tariff,
                "total": excl * (1 + self.vat)}

    def price(self, ts):
        p = self.parts(ts)
        return p["total"] if p else None


# --------------------------------------------------------------- estimat ----

def estimate_ev_from_meter(meter_rows, detect_kw=5.0, min_minutes=30):
    """Skøn elbil-opladning ud fra målerdata alene (bruges for perioder før
    Wall Connector-logningen startede). Returnerer {start: ev_kwh}."""
    out = {}
    by_day = OrderedDict()
    for r in meter_rows:
        by_day.setdefault(local_dt(r["start"]).date(), []).append(r)
    for rows in by_day.values():
        kw = [r["kwh"] * 3600 / r["seconds"] for r in rows]
        high = [k >= detect_kw for k in kw]
        low = [k for k, h in zip(kw, high) if not h]
        baseline = statistics.median(low) if low else 0.5
        i = 0
        while i < len(rows):
            if not high[i]:
                i += 1
                continue
            j = i
            while j < len(rows) and high[j]:
                j += 1
            minutes = sum(r["seconds"] for r in rows[i:j]) / 60
            if minutes >= min_minutes:
                for r, k in zip(rows[i:j], kw[i:j]):
                    out[r["start"]] = max(k - baseline, 0) * r["seconds"] / 3600
            i = j
    return out


# ------------------------------------------------------------ opsummering ----

def _group_key(ts, group):
    d = local_dt(ts)
    return d.strftime("%Y-%m-%d") if group == "day" else d.strftime("%Y-%m")


def summarize(meter_rows, ev_rows, prices, group="day", ev_logging_from=None,
              detect_kw=5.0):
    """Returnerer liste af perioder med kWh og kr for elbil og husstand."""
    ev = {r["start"]: r["kwh"] for r in ev_rows}
    groups = OrderedDict()

    def g(ts):
        k = _group_key(ts, group)
        if k not in groups:
            groups[k] = {"periode": k, "elbil_kwh": 0.0, "elbil_kr": 0.0,
                         "hus_kwh": 0.0, "hus_kr": 0.0, "total_kwh": 0.0,
                         "maaler_intervaller": 0, "elbil_estimeret": False,
                         "mangler_pris": False}
        return groups[k]

    # Perioder uden Wall Connector-data -> skøn ud fra måleren
    if ev_logging_from is None:
        early = meter_rows
    else:
        early = [r for r in meter_rows if r["start"] + r["seconds"] <= ev_logging_from]
    estimated = estimate_ev_from_meter(early, detect_kw) if early else {}

    used_ev = set()
    for r in meter_rows:
        start, sec, kwh = r["start"], r["seconds"], r["kwh"]
        row = g(start)
        row["maaler_intervaller"] += 1
        row["total_kwh"] += kwh
        buckets = list(range(start, start + sec, INTERVAL))
        if start in estimated:
            ev_kwh = min(estimated[start], kwh)
            row["elbil_estimeret"] = True
            ev_parts = [(b, ev_kwh / len(buckets)) for b in buckets]
        else:
            ev_parts = [(b, ev.get(b, 0.0)) for b in buckets]
            used_ev.update(buckets)
            ev_sum = sum(k for _, k in ev_parts)
            if ev_sum > kwh > 0:  # Wall Connector måler lidt mere end elmåleren
                ev_parts = [(b, k * kwh / ev_sum) for b, k in ev_parts]
        ev_kwh = sum(k for _, k in ev_parts)
        house = max(kwh - ev_kwh, 0.0)
        bucket_prices = [prices.price(b) for b in buckets]
        if any(p is None for p in bucket_prices):
            row["mangler_pris"] = True
            bucket_prices = [p for p in bucket_prices if p is not None] or [0.0]
        avg_price = sum(bucket_prices) / len(bucket_prices)
        row["elbil_kwh"] += ev_kwh
        for b, k in ev_parts:
            p = prices.price(b)
            row["elbil_kr"] += k * (p if p is not None else avg_price)
        row["hus_kwh"] += house
        row["hus_kr"] += house * avg_price

    # Opladning som elmåleren endnu ikke har leveret data for (typisk 1-2 døgn)
    for b, k in ev.items():
        if b in used_ev:
            continue
        row = g(b)
        row["elbil_kwh"] += k
        row["total_kwh"] += k
        p = prices.price(b)
        if p is None:
            row["mangler_pris"] = True
        else:
            row["elbil_kr"] += k * p

    out = []
    for row in groups.values():
        for key in ("elbil_kwh", "elbil_kr", "hus_kwh", "hus_kr", "total_kwh"):
            row[key] = round(row[key], 3 if key.endswith("kwh") else 2)
        row["total_kr"] = round(row["elbil_kr"] + row["hus_kr"], 2)
        row["maaler_data"] = row.pop("maaler_intervaller") > 0
        out.append(row)
    return sorted(out, key=lambda r: r["periode"])


def add_subscriptions(rows, group, monthly_kr):
    """Læg faste abonnementer (kr/måned inkl. moms) på hver periode.
    For dage fordeles månedsbeløbet ligeligt på månedens dage."""
    for r in rows:
        if group == "month":
            sub = monthly_kr
        else:
            y, m = int(r["periode"][:4]), int(r["periode"][5:7])
            sub = monthly_kr / calendar.monthrange(y, m)[1]
        r["abonnement_kr"] = round(sub, 2)
        r["total_inkl_abonnement_kr"] = round(r["total_kr"] + sub, 2)
    return rows


# ---------------------------------------------------------- ladesessioner ----

def sessions(ev_rows, prices, min_kwh=0.5, max_gap=1800, window_h=14):
    """Saml sammenhængende opladning til sessioner og beregn, hvad den samme
    mængde strøm ville have kostet i de billigste kvarterer efter tilslutning."""
    rows = [r for r in ev_rows if r["kwh"] > 0.01]
    groups, cur = [], []
    for r in rows:
        if cur and r["start"] - cur[-1]["start"] > max_gap:
            groups.append(cur)
            cur = []
        cur.append(r)
    if cur:
        groups.append(cur)

    out = []
    for grp in groups:
        kwh = sum(r["kwh"] for r in grp)
        if kwh < min_kwh:
            continue
        cost = sum(r["kwh"] * (prices.price(r["start"]) or 0) for r in grp)
        start, end = grp[0]["start"], grp[-1]["start"] + INTERVAL
        rate = max(r["kwh"] for r in grp)  # kWh pr. kvarter ved fuld effekt
        window = [(prices.price(t), t) for t in range(start, start + window_h * 3600, INTERVAL)
                  if prices.price(t) is not None]
        window.sort()
        need, best = kwh, 0.0
        for p, _ in window:
            take = min(rate, need)
            best += take * p
            need -= take
            if need <= 1e-9:
                break
        optimal = round(best, 2) if need <= 1e-9 else None
        out.append({
            "start": start, "slut": end,
            "kwh": round(kwh, 2), "kr": round(cost, 2),
            "kr_pr_kwh": round(cost / kwh, 2),
            "billigst_mulige_kr": optimal,
            "mulig_besparelse_kr": round(cost - optimal, 2) if optimal is not None else None,
        })
    return out


def cheapest_block(prices, t0, t1, hours):
    """Find billigste sammenhængende blok på `hours` timer i [t0, t1)."""
    slots = [(t, prices.price(t)) for t in range(t0 - t0 % INTERVAL, t1, INTERVAL)]
    slots = [(t, p) for t, p in slots if p is not None]
    n = max(1, int(round(hours * 3600 / INTERVAL)))
    best = None
    for i in range(0, len(slots) - n + 1):
        blk = slots[i:i + n]
        if blk[-1][0] - blk[0][0] != (n - 1) * INTERVAL:
            continue
        avg = sum(p for _, p in blk) / n
        if best is None or avg < best["gns_kr_pr_kwh"]:
            best = {"start": blk[0][0], "slut": blk[-1][0] + INTERVAL,
                    "gns_kr_pr_kwh": round(avg, 3)}
    return best

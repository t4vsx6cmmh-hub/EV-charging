"""Solceller: prognose for produktionen og hvad solstrøm "koster" at bruge selv.

Produktionen skønnes ud fra vejrudsigten (Open-Meteo, gratis og uden login):
indstråling på panelernes hældning og retning × anlæggets størrelse (kWp) ×
virkningsgrad, med et lille tab når panelerne er varme. Det virker med alle
invertere; en direkte forbindelse til inverteren kan lægges ovenpå senere.

Solstrøm, du selv bruger, koster det, du ellers ville have fået for at sælge
den: spotprisen minus elselskabets gebyr for at købe den (uden moms og afgifter).
"""
import urllib.parse

from .net import request_json
from .timeutil import INTERVAL

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Retninger: liste af (azimut, andel af anlægget). Open-Meteo: 0 = syd, -90 = øst, 90 = vest
ORIENTATIONS = {
    "syd": [(0, 1.0)], "sydoest": [(-45, 1.0)], "sydvest": [(45, 1.0)],
    "oest": [(-90, 1.0)], "vest": [(90, 1.0)], "oest-vest": [(-90, 0.5), (90, 0.5)],
}
ORIENTATION_NAMES = {"syd": "Syd", "sydoest": "Sydøst", "sydvest": "Sydvest",
                     "oest": "Øst", "vest": "Vest", "oest-vest": "Øst/vest"}
# Standardplacering pr. prisområde, hvis der ikke er angivet en
DEFAULT_LOCATION = {"DK1": (56.16, 10.20), "DK2": (55.68, 12.57)}


def enabled(cfg):
    return float(cfg.get("solar_kwp") or 0) > 0


def location(cfg):
    if cfg.get("solar_lat") is not None and cfg.get("solar_lon") is not None:
        return float(cfg["solar_lat"]), float(cfg["solar_lon"])
    return DEFAULT_LOCATION[cfg["price_area"]]


def kw_from_irradiance(wm2, temp_c, kwp, pr):
    """Effekt (kW) ud fra indstråling på panelet (W/m²) og lufttemperatur."""
    if wm2 <= 0:
        return 0.0
    cell = (temp_c if temp_c is not None else 10.0) + 25.0 * wm2 / 800.0
    derate = 1.0 - 0.004 * max(cell - 25.0, 0.0)
    return max(kwp * wm2 / 1000.0 * pr * derate, 0.0)


def parse_forecast(res, kwp, pr):
    """Open-Meteo timeværdier -> rækker (start, 3600, kW). Strålingen er gennemsnit
    for den foregående time, så tidsstemplet T dækker [T-3600, T)."""
    h = res.get("hourly") or {}
    times = h.get("time") or []
    gti = h.get("global_tilted_irradiance") or []
    temp = h.get("temperature_2m") or [None] * len(times)
    rows = []
    for t, g, c in zip(times, gti, temp):
        if g is None:
            continue
        rows.append((int(t) - 3600, 3600, round(kw_from_irradiance(float(g), c, kwp, pr), 3)))
    return rows


def fetch_forecast(cfg, base_url=FORECAST_URL):
    """Hent prognose for i går til og med i overmorgen; flere retninger lægges sammen."""
    lat, lon = location(cfg)
    kwp, pr = float(cfg["solar_kwp"]), float(cfg["solar_efficiency"])
    total = {}
    for azimuth, share in ORIENTATIONS[cfg["solar_orientation"]]:
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "tilt": cfg["solar_tilt"], "azimuth": azimuth,
            "hourly": "global_tilted_irradiance,temperature_2m",
            "timezone": "GMT", "timeformat": "unixtime", "past_days": 1, "forecast_days": 3,
        })
        for start, sec, kw in parse_forecast(request_json(f"{base_url}?{q}", timeout=30), kwp * share, pr):
            total[start] = total.get(start, 0.0) + kw
    return [(t, 3600, round(kw, 3)) for t, kw in sorted(total.items())]


class SolarForecast:
    """Produktion pr. kvarter (kW) ud fra de gemte timeværdier."""

    def __init__(self, rows):
        self.kw = {}
        for r in rows:
            start, sec, kw = (r["start"], r["seconds"], r["kw"]) if isinstance(r, dict) else r
            for t in range(start, start + sec, INTERVAL):
                self.kw[t] = kw

    def kw_at(self, ts):
        return self.kw.get(ts - ts % INTERVAL)


def sell_value(parts, fee):
    """Hvad 1 kWh solstrøm ellers kunne sælges for (kr/kWh)."""
    return None if parts is None else parts["spot"] - fee


class ChargePrices:
    """Prisen pr. kWh for at lade bilen i et kvarter, når solens overskud dækker en
    del: en blanding af salgsværdien (solstrøm) og den fulde købspris (nettet)."""

    def __init__(self, pm, forecast, charge_kw, base_kw, fee):
        self.pm, self.fc = pm, forecast
        self.charge_kw, self.base_kw, self.fee = charge_kw, base_kw, fee

    def surplus_kw(self, ts):
        kw = self.fc.kw_at(ts)
        return None if kw is None else max(kw - self.base_kw, 0.0)

    def solar_share(self, ts):
        s = self.surplus_kw(ts)
        return 0.0 if not s else min(s / self.charge_kw, 1.0)

    def price(self, ts):
        p = self.pm.price(ts)
        if p is None:
            return None
        share = self.solar_share(ts)
        if share <= 0:
            return p
        return share * sell_value(self.pm.parts(ts), self.fee) + (1 - share) * p

    def parts(self, ts):
        return self.pm.parts(ts)


class EvPrices:
    """Prisen for elbilens faktiske forbrug: den del, elmåleren ikke har købt fra
    nettet i kvarteret, er kommet fra solcellerne og koster salgsværdien."""

    def __init__(self, pm, meter_rows, ev_rows, fee):
        self.grid, self.fee = pm, fee
        self.meter = {}
        for r in meter_rows:
            n = max(r["seconds"] // INTERVAL, 1)
            for i in range(n):
                self.meter[r["start"] + i * INTERVAL] = r["kwh"] / n
        self.ev = {r["start"]: r["kwh"] for r in ev_rows}

    def solar_kwh(self, ts):
        b = ts - ts % INTERVAL
        e, m = self.ev.get(b, 0.0), self.meter.get(b)
        return 0.0 if m is None or e <= m else e - m

    def price(self, ts):
        p = self.grid.price(ts)
        b = ts - ts % INTERVAL
        e = self.ev.get(b, 0.0)
        sol = self.solar_kwh(b)
        if p is None or e <= 0 or sol <= 0:
            return p
        return (sol * sell_value(self.grid.parts(b), self.fee) + (e - sol) * p) / e

    def parts(self, ts):
        return self.grid.parts(ts)

"""Klient til Eloverblik.dk (Energinets DataHub) – kundens eget API.

Du laver selv et 'refresh token' på eloverblik.dk (log ind med MitID ->
'Del data' / 'Datadeling' -> 'Opret token'). Det gælder i op til et år.
Appen veksler det til et 'access token' (gyldigt 24 timer).
"""
import json
import time
import urllib.request
from datetime import datetime

from .timeutil import iso_duration_seconds, parse_iso_utc

BASE = "https://api.eloverblik.dk/customerapi/api"


class Eloverblik:
    def __init__(self, refresh_token):
        self.refresh_token = refresh_token.strip()
        self._access = None
        self._access_ts = 0

    def _req(self, method, path, token, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def access_token(self):
        if self._access and time.time() - self._access_ts < 20 * 3600:
            return self._access
        self._access = self._req("GET", "/token", self.refresh_token)["result"]
        self._access_ts = time.time()
        return self._access

    def metering_points(self):
        res = self._req("GET", "/meteringpoints/meteringpoints?includeAll=false",
                        self.access_token())
        return res.get("result", [])

    def first_consumption_point(self):
        for mp in self.metering_points():
            if mp.get("typeOfMP", "E17") == "E17":  # E17 = forbrug
                return mp["meteringPointId"]
        raise RuntimeError("Fandt intet forbrugsmålepunkt på Eloverblik")

    def timeseries(self, mpid, date_from, date_to, aggregation="Actual"):
        path = (f"/meterdata/gettimeseries/{date_from.isoformat()}/"
                f"{date_to.isoformat()}/{aggregation}")
        body = {"meteringPoints": {"meteringPoint": [mpid]}}
        return parse_timeseries(self._req("POST", path, self.access_token(), body))

    def charges(self, mpid):
        body = {"meteringPoints": {"meteringPoint": [mpid]}}
        res = self._req("POST", "/meteringpoints/meteringpoint/getcharges",
                        self.access_token(), body)
        return parse_charges(res)


def parse_timeseries(res):
    """Eloverblik-svar -> rækker (unix_start, sekunder, kWh)."""
    rows = []
    for item in res.get("result", []):
        doc = item.get("MyEnergyData_MarketDocument") or {}
        for ts in doc.get("TimeSeries", []) or []:
            for period in ts.get("Period", []) or []:
                start = parse_iso_utc(period["timeInterval"]["start"])
                sec = iso_duration_seconds(period.get("resolution", "PT1H"))
                for p in period.get("Point", []) or []:
                    pos = int(p["position"])
                    qty = float(p.get("out_Quantity.quantity", 0) or 0)
                    rows.append((start + (pos - 1) * sec, sec, qty))
    return rows


def parse_charges(res):
    """Udtræk tariffer (kr/kWh ekskl. moms) i et simpelt format:
    [{"name":..., "owner":..., "prices":[pris pr. tidsslot over døgnet]}]."""
    out = []
    for item in res.get("result", []):
        r = item.get("result") or {}
        for t in r.get("tariffs", []) or []:
            prices = sorted(t.get("prices", []) or [], key=lambda p: int(p["position"]))
            out.append({
                "name": t.get("name", ""),
                "owner": t.get("owner", ""),
                "prices": [float(p["price"]) for p in prices],
                "fetched": datetime.now().isoformat(timespec="seconds"),
            })
    return out

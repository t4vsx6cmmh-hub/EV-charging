"""Spotpriser fra Energi Data Service (Energinet) – gratis, uden login.

Fra 1. oktober 2025 handles elmarkedet i 15-minutters intervaller og priserne
ligger i datasættet 'DayAheadPrices'. Ældre timepriser ligger i 'Elspotprices'.
"""
import json
import urllib.parse
import urllib.request
from datetime import date

from .timeutil import parse_iso_utc

BASE = "https://api.energidataservice.dk/dataset/"
CUTOVER = date(2025, 10, 1)


def _fetch(dataset, start, end, area):
    q = urllib.parse.urlencode({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "filter": json.dumps({"PriceArea": [area]}),
        "sort": "TimeUTC asc" if dataset == "DayAheadPrices" else "HourUTC asc",
        "limit": 0,
    })
    req = urllib.request.Request(BASE + dataset + "?" + q,
                                 headers={"User-Agent": "elopladning-til-biler"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("records", [])


def parse_dayahead(records):
    rows = []
    for rec in records:
        price = rec.get("DayAheadPriceDKK")
        if price is None:
            continue
        rows.append((parse_iso_utc(rec["TimeUTC"]), 900, float(price)))
    return rows


def parse_elspot(records):
    rows = []
    for rec in records:
        price = rec.get("SpotPriceDKK")
        if price is None and rec.get("SpotPriceEUR") is not None:
            price = float(rec["SpotPriceEUR"]) * 7.46
        if price is None:
            continue
        rows.append((parse_iso_utc(rec["HourUTC"]), 3600, float(price)))
    return rows


def fetch_spot(start, end, area="DK2"):
    """Hent spotpriser [start, end) som rækker (unix_start, sekunder, kr/MWh)."""
    rows = []
    if start < CUTOVER:
        rows += parse_elspot(_fetch("Elspotprices", start, min(end, CUTOVER), area))
    if end > CUTOVER:
        rows += parse_dayahead(_fetch("DayAheadPrices", max(start, CUTOVER), end, area))
    return rows

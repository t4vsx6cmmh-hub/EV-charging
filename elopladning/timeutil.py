"""Tidshjælpere. Alt gemmes som unix-tid (UTC); visning sker i dansk tid."""
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    DK = ZoneInfo("Europe/Copenhagen")
except Exception:  # pragma: no cover - fx Windows uden tzdata
    raise SystemExit("Mangler tidszonedata. Kør: pip install tzdata")

INTERVAL = 900  # 15 minutter


def parse_iso_utc(s):
    """'2025-10-01T00:00:00Z' / '2025-10-01T00:00:00' (UTC) -> unix-tid."""
    s = s.replace("Z", "")
    if "+" in s[10:]:
        return int(datetime.fromisoformat(s).timestamp())
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def iso_duration_seconds(res):
    return {"PT15M": 900, "PT1H": 3600, "PT30M": 1800, "P1D": 86400}.get(res, 3600)


def local_dt(ts):
    return datetime.fromtimestamp(ts, DK)


def local_midnight(day):
    """day: date -> unix-tid for midnat i dansk tid."""
    return int(datetime(day.year, day.month, day.day, tzinfo=DK).timestamp())


def day_range(day):
    nxt = day + timedelta(days=1)
    return local_midnight(day), local_midnight(nxt)


def month_range(year, month):
    start = datetime(year, month, 1, tzinfo=DK)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=DK)
    return int(start.timestamp()), int(end.timestamp())

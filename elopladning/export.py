"""CSV-eksport, der åbner direkte i dansk Excel/Numbers (semikolon og decimalkomma)."""
import csv
import io

from .timeutil import local_dt

PERIOD_COLUMNS = [
    ("periode", "Periode", None),
    ("elbil_kwh", "Elbil kWh", 3),
    ("elbil_kr", "Elbil kr", 2),
    ("hus_kwh", "Hus kWh", 3),
    ("hus_kr", "Hus kr", 2),
    ("total_kwh", "I alt kWh", 3),
    ("total_kr", "I alt kr (forbrug)", 2),
    ("abonnement_kr", "Abonnementer kr", 2),
    ("total_inkl_abonnement_kr", "I alt kr inkl. abonnementer", 2),
    ("elbil_estimeret", "Elbil skønnet", None),
    ("maaler_data", "Målerdata modtaget", None),
    ("mangler_pris", "Mangler pris", None),
]

SESSION_COLUMNS = [
    ("start_tekst", "Start", None),
    ("slut_tekst", "Slut", None),
    ("kwh", "kWh", 2),
    ("kr", "Pris kr", 2),
    ("kr_pr_kwh", "kr/kWh", 2),
    ("billigst_mulige_kr", "Billigst mulige kr", 2),
    ("mulig_besparelse_kr", "Mulig besparelse kr", 2),
]


def _fmt(v, decimals):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "ja" if v else "nej"
    if isinstance(v, (int, float)) and decimals is not None:
        return f"{v:.{decimals}f}".replace(".", ",")
    return str(v)


def _csv(rows, columns):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([c[1] for c in columns])
    for r in rows:
        w.writerow([_fmt(r.get(key), dec) for key, _, dec in columns])
    return "﻿" + buf.getvalue()  # BOM: Excel genkender UTF-8 (æøå)


def periods_csv(rows):
    return _csv(rows, PERIOD_COLUMNS)


def sessions_csv(sessions):
    rows = [dict(s, start_tekst=local_dt(s["start"]).strftime("%Y-%m-%d %H:%M"),
                 slut_tekst=local_dt(s["slut"]).strftime("%Y-%m-%d %H:%M"))
            for s in sessions]
    return _csv(rows, SESSION_COLUMNS)

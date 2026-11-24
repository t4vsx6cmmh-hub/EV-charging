"""Indstillinger fra appen: beskrivelse af felterne, gem i config.json og test forbindelser.

Hemmeligheder (tokens, adgangskoder) sendes aldrig tilbage til browseren – kun
om de er udfyldt. Et tomt felt betyder "behold det gemte"; null sletter det.
"""
import copy
import json
import os
import tempfile

from . import prices, solar, tesla, wallconnector
from .config import DEFAULTS, ConfigError, validate
from .eloverblik import Eloverblik

N, T, S, SEL, B, D = "number", "text", "secret", "select", "bool", "dict"

SECTIONS = [
    {"id": "wallconnector", "titel": "Wall Connector", "test": "wall-connector",
     "intro": "Laderens IP-adresse på hjemmenetværket. Find den i routerens liste over enheder "
              "(hedder typisk noget med \"TeslaWallConnector\"). Giv den gerne en fast IP i routeren.",
     "felter": [{"key": "wall_connector_host", "type": T, "label": "IP-adresse", "placeholder": "192.168.1.50"}]},
    {"id": "eloverblik", "titel": "Eloverblik", "test": "eloverblik",
     "intro": "Husets forbrug og dine tariffer. Log ind på eloverblik.dk med MitID → Datadeling → "
              "Opret token, og indsæt det her.",
     "link": "https://eloverblik.dk",
     "felter": [{"key": "eloverblik_refresh_token", "type": S, "label": "Token"},
                {"key": "metering_point_id", "type": T, "label": "Målepunkt",
                 "hjaelp": "Tom = vælg automatisk dit forbrugsmålepunkt. Tryk Test for at se dine målepunkter."}]},
    {"id": "priser", "titel": "Elpris og elselskab", "test": "spot",
     "intro": "Spotpriser hentes gratis fra Energinet. Elselskabets tillæg står på din regning.",
     "felter": [{"key": "price_area", "type": SEL, "label": "Prisområde",
                 "valg": [["DK1", "Vestdanmark (Jylland, Fyn)"], ["DK2", "Østdanmark (Sjælland, øerne)"]]},
                {"key": "supplier_addon_kr_per_kwh", "type": N, "label": "Elselskabets tillæg",
                 "enhed": "kr/kWh uden moms", "step": 0.001,
                 "hjaelp": "Fx 6 øre inkl. moms = 0,048 kr uden moms."},
                {"key": "subscriptions_kr_per_month", "type": D, "label": "Faste abonnementer",
                 "enhed": "kr/md inkl. moms", "hjaelp": "Fx netabonnement og elselskabets abonnement."}]},
    {"id": "bil", "titel": "Bilen",
     "intro": "Bruges til at regne ladetid, km og sammenligningen med benzin.",
     "felter": [{"key": "charge_kw", "type": N, "label": "Ladeeffekt", "enhed": "kW", "step": 0.1,
                 "hjaelp": "11 kW ved 3 × 16 A."},
                {"key": "battery_kwh", "type": N, "label": "Batteristørrelse", "enhed": "kWh", "step": 1},
                {"key": "consumption_kwh_per_100km", "type": N, "label": "Forbrug", "enhed": "kWh/100 km", "step": 0.5},
                {"key": "petrol_l_per_100km", "type": N, "label": "Sammenlign med benzinbil", "enhed": "l/100 km", "step": 0.1},
                {"key": "petrol_price_kr_per_l", "type": N, "label": "Benzinpris", "enhed": "kr/l", "step": 0.1}]},
    {"id": "solceller", "titel": "Solceller", "test": "solar", "valgfri": True,
     "intro": "Med solceller lader smart opladning gerne på dit eget overskud. Produktionen skønnes ud fra "
              "vejrudsigten, så det virker med alle invertere. Anlæggets størrelse (kWp) står i papirerne "
              "fra installatøren eller i inverter-appen.",
     "felter": [{"key": "solar_kwp", "type": N, "label": "Anlæggets størrelse", "enhed": "kWp", "step": 0.1,
                 "hjaelp": "0 = ingen solceller."},
                {"key": "solar_orientation", "type": SEL, "label": "Panelernes retning",
                 "valg": [[k, v] for k, v in solar.ORIENTATION_NAMES.items()]},
                {"key": "solar_tilt", "type": N, "label": "Hældning", "enhed": "grader", "step": 1,
                 "hjaelp": "Typisk tag: 25–45°."},
                {"key": "solar_lat", "type": N, "label": "Breddegrad", "step": 0.01, "valgfri": True,
                 "hjaelp": "Tom = midt i dit prisområde."},
                {"key": "solar_lon", "type": N, "label": "Længdegrad", "step": 0.01, "valgfri": True},
                {"key": "solar_sell_fee_kr_per_kwh", "type": N, "label": "Gebyr ved salg",
                 "enhed": "kr/kWh", "step": 0.001,
                 "hjaelp": "Det elselskabet trækker fra spotprisen, når du sælger strøm."},
                {"key": "solar_base_load_kw", "type": N, "label": "Husets grundforbrug i dagtimerne",
                 "enhed": "kW", "step": 0.1}]},
    {"id": "tesla", "titel": "Tesla (styring af bilen)", "valgfri": True,
     "intro": "Valgfrit: lad appen selv starte og stoppe opladningen og læse batteriet. Kræver en gratis app "
              "på developer.tesla.com (se README).",
     "link": "https://developer.tesla.com",
     "felter": [{"key": "tesla_client_id", "type": T, "label": "Client ID"},
                {"key": "tesla_client_secret", "type": S, "label": "Client secret"},
                {"key": "tesla_redirect_uri", "type": T, "label": "Redirect URL",
                 "hjaelp": "Brug appens egen adresse + /tesla/callback, fx https://pi.tail1234.ts.net/tesla/callback, "
                           "og skriv den samme hos Tesla."},
                {"key": "tesla_region", "type": SEL, "label": "Region", "valg": [["eu", "Europa"], ["na", "Nordamerika"]]},
                {"key": "tesla_command_proxy", "type": T, "label": "Kommando-proxy (nyere biler)",
                 "placeholder": "https://localhost:4443"},
                {"key": "tesla_proxy_ca_file", "type": T, "label": "Proxyens certifikat (fil)"}]},
    {"id": "notifikationer", "titel": "Notifikationer", "test": "ntfy", "valgfri": True,
     "intro": "Beskeder på telefonen via den gratis app ntfy. Vælg et hemmeligt emne, abonnér på det i appen, "
              "og skriv det her.",
     "link": "https://ntfy.sh",
     "felter": [{"key": "ntfy_topic", "type": T, "label": "Emne", "placeholder": "elopladning-k8f2x9q"},
                {"key": "ntfy_server", "type": T, "label": "Server"},
                {"key": "ntfy_token", "type": S, "label": "Adgangstoken (kun egen server)"},
                {"key": "notify_session_end", "type": B, "label": "Når opladningen stopper"},
                {"key": "notify_smart_plan", "type": B, "label": "Når smart-planen er lagt"},
                {"key": "notify_tomorrow_prices", "type": B, "label": "Når morgendagens priser er ude"},
                {"key": "notify_errors", "type": B, "label": "Hvis noget holder op med at virke"},
                {"key": "notify_price_below", "type": N, "label": "Fremhæv priser under", "enhed": "kr/kWh",
                 "step": 0.05, "valgfri": True}]},
]

FIELD_TYPES = {f["key"]: f["type"] for s in SECTIONS for f in s["felter"]}
LABELS = {f["key"]: f["label"] for s in SECTIONS for f in s["felter"]}


def readable(msg):
    """Brug feltets navn fra formularen i fejlbeskeder i stedet for config-nøglen."""
    for key in sorted(LABELS, key=len, reverse=True):
        msg = msg.replace(key + ":", LABELS[key] + ":")
    return msg.replace("  • ", "").replace("\n", " · ")
OPTIONAL = {f["key"] for s in SECTIONS for f in s["felter"] if f.get("valgfri")}


def section_status(cfg, app):
    """Kort status pr. afsnit til opsætningsguiden."""
    car_ok = app.car is not None
    demo = app.demo  # demoen har en simuleret lader og forbrugsdata
    return {
        "wallconnector": "klar" if cfg["wall_connector_host"] or demo else "mangler",
        "eloverblik": "klar" if cfg["eloverblik_refresh_token"] or demo else "mangler",
        "priser": "klar" if cfg["supplier_addon_kr_per_kwh"] else "tjek",
        "bil": "klar",
        "solceller": "klar" if solar.enabled(cfg) else "fra",
        "tesla": "klar" if car_ok else ("login" if cfg["tesla_client_id"] else "fra"),
        "notifikationer": "klar" if cfg["ntfy_topic"] else "fra",
    }


def public(cfg, app):
    values = {}
    for key, typ in FIELD_TYPES.items():
        v = cfg.get(key)
        values[key] = {"saet": bool(v)} if typ == S else v
    return {"afsnit": SECTIONS, "vaerdier": values, "status": section_status(cfg, app),
            "kan_gemme": bool(app.config_path) and not app.demo,
            "foerste_start": not cfg["wall_connector_host"] and not cfg["eloverblik_refresh_token"],
            "tesla_login": bool(cfg["tesla_client_id"] and cfg["tesla_client_secret"] and cfg["tesla_redirect_uri"])}


def _coerce(key, value):
    typ = FIELD_TYPES[key]
    if value is None:
        if typ in (T, S):
            return ""
        if key in OPTIONAL:
            return None
        raise ConfigError(f"  • {key}: skal udfyldes")
    if typ == N:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
            if value == "":
                if key in OPTIONAL:
                    return None
                raise ConfigError(f"  • {key}: skal udfyldes")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"  • {key}: skal være et tal") from None
        return int(value) if DEFAULTS.get(key) is not None and isinstance(DEFAULTS[key], int) \
            and not isinstance(DEFAULTS[key], bool) and value == int(value) else value
    if typ == B:
        return bool(value)
    if typ == D:
        if not isinstance(value, dict):
            raise ConfigError(f"  • {key}: forkert format")
        return {str(k).strip(): float(str(v).replace(",", ".")) for k, v in value.items() if str(k).strip()}
    return str(value).strip()


def merge(cfg, updates):
    """Returnér (ny config, ændrede nøgler). Kaster ConfigError ved fejl."""
    if not isinstance(updates, dict):
        raise ConfigError("  • forventede et objekt med felter")
    unknown = [k for k in updates if k not in FIELD_TYPES]
    if unknown:
        raise ConfigError("  • kan ikke ændres her: " + ", ".join(unknown))
    new = copy.deepcopy(cfg)
    changed = {}
    for key, value in updates.items():
        if FIELD_TYPES[key] == S and value == "":
            continue  # tomt hemmeligt felt = behold det gemte
        v = _coerce(key, value)
        if new.get(key) != v:
            new[key] = v
            changed[key] = v
    validate(new)
    return new, changed


def write_config(path, changed):
    """Skriv kun de ændrede felter i config.json; resten (også kommentarer og
    avancerede felter) bevares. Skrives atomisk, så filen aldrig bliver halv."""
    raw = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
    raw.update(changed)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        else:
            os.chmod(tmp, 0o600)  # indeholder tokens
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ test --

def _fmt(v, d=1):
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def test_connection(kind, cfg, body):
    """Prøv en forbindelse med de værdier, der står i formularen (eller de gemte)."""
    c = copy.deepcopy(cfg)
    for k, v in (body or {}).items():
        if k in FIELD_TYPES and not (FIELD_TYPES[k] == S and v == ""):
            try:
                c[k] = _coerce(k, v)
            except ConfigError:
                pass
    try:
        if kind == "wall-connector":
            host = c["wall_connector_host"]
            if not host:
                return {"ok": False, "tekst": "Skriv IP-adressen først."}
            total = wallconnector.read_lifetime_wh(host) / 1000
            v = wallconnector.read_vitals(host)
            car = "bil tilsluttet" if v.get("vehicle_connected") else "ingen bil tilsluttet"
            return {"ok": True, "tekst": f"Forbundet – har leveret {_fmt(total, 0)} kWh i alt, {car}."}
        if kind == "eloverblik":
            if not c["eloverblik_refresh_token"]:
                return {"ok": False, "tekst": "Indsæt tokenet først."}
            points = Eloverblik(c["eloverblik_refresh_token"]).metering_points()
            out = [{"id": p.get("meteringPointId"), "type": p.get("typeOfMP"),
                    "adresse": " ".join(str(p.get(k) or "") for k in
                                        ("streetName", "buildingNumber", "postcode", "cityName")).strip()}
                   for p in points]
            kinds = {"E17": "forbrug", "E18": "produktion (solceller)", "D07": "ladestander"}
            txt = ", ".join(f"{p['adresse'] or p['id']} ({kinds.get(p['type'], p['type'])})" for p in out)
            return {"ok": True, "tekst": f"Forbundet – {len(out)} målepunkt(er): {txt}", "maalepunkter": out}
        if kind == "spot":
            from datetime import datetime, timedelta
            from .timeutil import DK
            today = datetime.now(DK).date()
            rows = prices.fetch_spot(today, today + timedelta(days=1), c["price_area"])
            if not rows:
                return {"ok": False, "tekst": "Energinet svarede, men uden priser for i dag."}
            avg = sum(r[2] for r in rows) / len(rows) / 1000
            return {"ok": True, "tekst": f"Spotpriser hentet – i dag gns. {_fmt(avg, 2)} kr/kWh uden afgifter."}
        if kind == "solar":
            if not solar.enabled(c):
                return {"ok": False, "tekst": "Skriv anlæggets størrelse (kWp) først."}
            rows = solar.fetch_forecast(c)
            fc = solar.SolarForecast(rows)
            from datetime import datetime, timedelta
            from .timeutil import DK, local_midnight
            tomorrow = datetime.now(DK).date() + timedelta(days=1)
            t0 = local_midnight(tomorrow)
            kwh = sum((fc.kw_at(t) or 0) for t in range(t0, t0 + 86400, 900)) / 4
            peak = max([(fc.kw_at(t) or 0) for t in range(t0, t0 + 86400, 900)] or [0])
            return {"ok": True, "tekst": f"Vejrudsigt hentet – i morgen ca. {_fmt(kwh)} kWh, "
                                         f"højst {_fmt(peak)} kW."}
        if kind == "ntfy":
            if not c["ntfy_topic"]:
                return {"ok": False, "tekst": "Skriv et emne først."}
            from .notify import Ntfy
            Ntfy(c["ntfy_topic"], c["ntfy_server"], c["ntfy_token"]).send(
                "Elopladning til biler", "Testbesked – notifikationer virker.", ["white_check_mark"])
            return {"ok": True, "tekst": "Testbesked sendt – tjek telefonen."}
    except Exception as e:
        return {"ok": False, "tekst": friendly_error(kind, c, e)}
    return {"ok": False, "tekst": "Ukendt test."}


def friendly_error(kind, cfg, e):
    """Oversæt tekniske fejl til noget, man kan handle på."""
    import socket
    import urllib.error
    from .net import HTTPError
    reason = getattr(e, "reason", None)
    if kind == "wall-connector" and isinstance(e, (OSError, urllib.error.URLError)):
        if isinstance(reason, socket.timeout) or isinstance(e, socket.timeout) or "timed out" in str(e):
            return (f"Ingen svar fra {cfg['wall_connector_host']} – tjek IP-adressen, og at computeren "
                    "er på samme netværk som laderen.")
        return (f"Kunne ikke få forbindelse til {cfg['wall_connector_host']} – tjek IP-adressen. "
                "Prøv også at åbne http://" + cfg["wall_connector_host"] + "/api/1/vitals i browseren.")
    if isinstance(e, HTTPError) and e.status in (401, 403):
        return "Adgang nægtet – tjek token/nøgle."
    if isinstance(e, urllib.error.URLError) or isinstance(e, OSError):
        return f"Ingen forbindelse til internettet eller tjenesten ({reason or e})."
    return str(e) or type(e).__name__


def tesla_status(app):
    cfg = app.cfg
    return {"klar_til_login": bool(cfg["tesla_client_id"] and cfg["tesla_client_secret"]
                                   and cfg["tesla_redirect_uri"]),
            "logget_ind": tesla.configured(cfg, app.db), "styrer": app.car is not None}

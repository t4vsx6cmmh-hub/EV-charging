"""Indlæsning og kontrol af config.json."""
import copy
import json
import logging
import os
import re

log = logging.getLogger("elopladning")

DEFAULTS = {
    # Data
    "wall_connector_host": "",
    "eloverblik_refresh_token": "",
    "metering_point_id": "",
    "price_area": "DK2",
    "supplier_addon_kr_per_kwh": 0.0,
    "vat": 0.25,
    "manual_tariffs": None,
    "subscriptions_kr_per_month": {},
    "poll_seconds": 30,
    "history_days": 90,
    "ev_detect_kw": 5.0,
    # Server
    "listen_host": "0.0.0.0",
    "port": 8080,
    "db_path": "elopladning.db",
    "tls_cert_file": "",
    "tls_key_file": "",
    "allowed_origins": [],
    # Smart opladning
    "charge_kw": 11.0,
    "battery_kwh": 75.0,
    "charge_efficiency": 0.9,
    "smart_default_ready": "07:00",
    "smart_default_kwh": 30,
    # Tesla Fleet API (valgfrit – kræves kun for at appen selv kan starte/stoppe)
    "tesla_client_id": "",
    "tesla_client_secret": "",
    "tesla_redirect_uri": "",
    "tesla_refresh_token": "",
    "tesla_vin": "",
    "tesla_region": "eu",
    "tesla_api_base": "",
    "tesla_command_proxy": "",
    "tesla_proxy_ca_file": "",
    # Notifikationer via ntfy
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_token": "",
    "notify_session_end": True,
    "notify_smart_plan": True,
    "notify_tomorrow_prices": True,
    "notify_block_hours": 4,
    "notify_price_below": None,
    "notify_errors": True,
}


class ConfigError(ValueError):
    pass


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(cfg):
    """Tjek værdierne og giv en samlet, letlæselig fejlbesked."""
    errs = []

    def num(key, lo=None, hi=None, integer=False, optional=False):
        v = cfg.get(key)
        if v is None and optional:
            return
        if not _is_num(v) or (integer and float(v) != int(v)):
            errs.append(f"{key}: skal være et {'heltal' if integer else 'tal'} (er {v!r})")
            return
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            errs.append(f"{key}: skal ligge mellem {lo} og {hi} (er {v})")
        elif integer:
            cfg[key] = int(v)

    def text(key, pattern=None, choices=None):
        v = cfg.get(key)
        if not isinstance(v, str):
            errs.append(f"{key}: skal være tekst i anførselstegn (er {v!r})")
        elif choices and v not in choices:
            errs.append(f"{key}: skal være en af {', '.join(choices)} (er {v!r})")
        elif pattern and not re.fullmatch(pattern, v):
            errs.append(f"{key}: ugyldig værdi {v!r}")

    for key in ("wall_connector_host", "eloverblik_refresh_token", "metering_point_id",
                "listen_host", "db_path", "tesla_client_id", "tesla_client_secret",
                "tesla_redirect_uri", "tesla_refresh_token", "tesla_vin", "tesla_api_base",
                "tesla_command_proxy", "tesla_proxy_ca_file", "ntfy_topic", "ntfy_server",
                "ntfy_token", "tls_cert_file", "tls_key_file"):
        text(key)
    if bool(cfg.get("tls_cert_file")) != bool(cfg.get("tls_key_file")):
        errs.append("tls_cert_file og tls_key_file skal udfyldes begge to (eller ingen af dem)")
    ao = cfg.get("allowed_origins")
    if not isinstance(ao, list) or not all(isinstance(o, str) and re.fullmatch(r"https?://[^/\s]+", o) for o in ao):
        errs.append('allowed_origins: skal være en liste som ["https://pi.mit-netvaerk.ts.net"] (uden / til sidst)')
    text("price_area", choices=("DK1", "DK2"))
    text("tesla_region", choices=("eu", "na"))
    text("smart_default_ready", pattern=r"([01]?\d|2[0-3]):[0-5]\d")
    if isinstance(cfg.get("wall_connector_host"), str) and "/" in cfg["wall_connector_host"]:
        errs.append("wall_connector_host: skriv kun IP-adressen, fx 192.168.1.50 (uden http://)")

    num("supplier_addon_kr_per_kwh", -5, 5)
    num("vat", 0, 1)
    num("poll_seconds", 5, 3600, integer=True)
    num("history_days", 1, 1000, integer=True)
    num("ev_detect_kw", 0.5, 50)
    num("port", 1, 65535, integer=True)
    num("charge_kw", 1, 22)
    num("battery_kwh", 10, 250)
    num("charge_efficiency", 0.5, 1)
    num("smart_default_kwh", 1, 150)
    num("notify_block_hours", 1, 12)
    num("notify_price_below", -5, 20, optional=True)
    for key in ("notify_session_end", "notify_smart_plan", "notify_tomorrow_prices", "notify_errors"):
        if not isinstance(cfg.get(key), bool):
            errs.append(f"{key}: skal være true eller false (er {cfg.get(key)!r})")

    subs = cfg.get("subscriptions_kr_per_month")
    if not isinstance(subs, dict) or not all(_is_num(v) for v in subs.values()):
        errs.append('subscriptions_kr_per_month: skal være fx {"Netabonnement": 25.0} med tal')

    mt = cfg.get("manual_tariffs")
    if mt is not None:
        if not isinstance(mt, dict):
            errs.append("manual_tariffs: skal være null eller et objekt { ... }")
        else:
            nh = mt.get("net_hourly")
            if nh is not None and (not isinstance(nh, list) or len(nh) != 24
                                   or not all(_is_num(x) for x in nh)):
                errs.append("manual_tariffs.net_hourly: skal være en liste med 24 tal (én pr. time)")
            for k, v in mt.items():
                if k != "net_hourly" and v is not None and not _is_num(v):
                    errs.append(f"manual_tariffs.{k}: skal være et tal")

    if errs:
        raise ConfigError("\n".join("  • " + e for e in errs))
    return cfg


def load_config(path):
    """Læs config.json (hvis den findes) oven på standardværdierne.

    En relativ db_path regnes i forhold til mappen med config-filen."""
    cfg = copy.deepcopy(DEFAULTS)
    base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            try:
                user = json.load(f)
            except json.JSONDecodeError as e:
                raise ConfigError(f"  • {os.path.basename(path)} er ikke gyldig JSON "
                                  f"(linje {e.lineno}, kolonne {e.colno}): {e.msg}") from None
        if not isinstance(user, dict):
            raise ConfigError("  • config.json skal være et JSON-objekt { ... }")
        unknown = sorted(k for k in user if k not in DEFAULTS and not k.startswith("_"))
        if unknown:
            log.warning("Ukendte felter i config.json ignoreres: %s", ", ".join(unknown))
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    validate(cfg)
    for key in ("db_path", "tls_cert_file", "tls_key_file"):
        if cfg[key]:
            cfg[key] = os.path.join(base, cfg[key])
    return cfg

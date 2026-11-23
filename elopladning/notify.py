"""Notifikationer til telefonen via ntfy (https://ntfy.sh – gratis app til iOS/Android).

Vælg et hemmeligt, svært gætteligt emne, fx "elopladning-k8f2x9", abonnér på
det i ntfy-appen og skriv det i config.json som `ntfy_topic`.
"""
import logging
from datetime import datetime, timedelta

from . import calc
from .net import request_json
from .smart import merge_periods
from .timeutil import DK, INTERVAL, day_range, local_dt
from .wallconnector import power_w

log = logging.getLogger("elopladning")

ERROR_NAMES = {"wall_connector": "Wall Connector", "eloverblik": "Eloverblik",
               "spotpriser": "Spotpriser"}
ERROR_AFTER = 2 * 3600


def dk(v, d=2):
    """1234.5 -> '1.234,50'"""
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hhmm(ts):
    return local_dt(ts).strftime("%H:%M")


class Ntfy:
    def __init__(self, topic, server="https://ntfy.sh", token=""):
        self.topic, self.server, self.token = topic, server.rstrip("/"), token

    def send(self, title, message, tags=(), priority=3):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # JSON-udgaven af ntfy klarer æøå og tankestreger i titlen
        request_json(self.server + "/", "POST", headers=headers, timeout=15, retries=1, body={
            "topic": self.topic, "title": title, "message": message,
            "tags": list(tags), "priority": priority,
        })


class LogNotifier:
    """Bruges i demo-tilstand: skriver i loggen i stedet for at sende."""
    def __init__(self):
        self.sent = []

    def send(self, title, message, tags=(), priority=3):
        self.sent.append((title, message))
        log.info("[notifikation] %s – %s", title, message.replace("\n", " / "))


def make_notifier(cfg, demo=False):
    if cfg["ntfy_topic"] and not demo:
        return Ntfy(cfg["ntfy_topic"], cfg["ntfy_server"], cfg["ntfy_token"])
    return LogNotifier() if demo else None


class Events:
    """Holder øje med opladning, priser og fejl og sender beskeder."""

    def __init__(self, app, notifier):
        self.app = app
        self.n = notifier
        self.was_charging = None
        self.plug_ts = None
        self.plan_sent = False
        self.notified_errors = set()

    def send(self, title, message, tags=(), priority=3):
        if not self.n:
            return False
        try:
            self.n.send(title, message, tags, priority)
            return True
        except Exception as e:
            log.warning("Notifikation kunne ikke sendes: %s", e)
            return False

    # ------------------------------------------------------------- opladning --
    def on_vitals(self, v, now):
        if v is None:
            return
        cfg, sm = self.app.cfg, self.app.smart
        connected = bool(v.get("vehicle_connected"))
        charging = bool(v.get("contactor_closed")) and power_w(v) > 100
        if connected and self.plug_ts is None:
            self.plug_ts = now

        if self.was_charging and not charging and cfg["notify_session_end"]:
            paused = (sm.last_cmd is False and now - sm.last_cmd_ts < 300
                      and (sm.remaining or 0) > 0.1)
            if not paused:
                self._session_done(now)

        if (connected and not self.plan_sent and cfg["notify_smart_plan"]
                and sm.plugged and sm.plan and sm.plan["slots"]
                and sm.settings()["mode"] == "smart"):
            self.plan_sent = True
            self.send("Smart opladning planlagt", self.plan_text(sm.plan), ["calendar"])

        if not connected:
            self.plug_ts, self.plan_sent = None, False
        self.was_charging = charging

    @staticmethod
    def plan_text(plan):
        per = ", ".join(f"{hhmm(a)}–{hhmm(b)}" for a, b in plan["perioder"])
        lines = [f"{dk(plan['kwh'], 1)} kWh: {per}"]
        if plan["kr"] is not None:
            line = f"Ca. {dk(plan['kr'])} kr"
            if plan["kr_hvis_nu"] and plan["kr_hvis_nu"] - plan["kr"] >= 0.5:
                line += f" – sparer ca. {dk(plan['kr_hvis_nu'] - plan['kr'])} kr"
            lines.append(line)
        if plan["naar_ikke"]:
            lines.append(f"Bemærk: der er ikke tid nok til at nå det hele inden {hhmm(plan['klar'])}")
        return "\n".join(lines)

    def _session_done(self, now):
        since = self.plug_ts or now - 24 * 3600
        rows = self.app.db.ev_intervals(since - INTERVAL, now + INTERVAL)
        kwh = sum(r["kwh"] for r in rows)
        if kwh < 0.5:
            return
        pm = self.app.price_model(since - INTERVAL, now + INTERVAL)
        kr = sum(r["kwh"] * (pm.price(r["start"]) or 0) for r in rows)
        self.send("Opladning stoppet",
                  f"{dk(kwh, 1)} kWh · {dk(kr)} kr ({dk(kr / kwh)} kr/kWh)",
                  ["electric_plug"])

    # ----------------------------------------------------------------- priser --
    def on_prices(self, now):
        cfg = self.app.cfg
        if not cfg["notify_tomorrow_prices"] or not self.n:
            return
        tomorrow = (datetime.now(DK) + timedelta(days=1)).date()
        if self.app.db.get_kv("notified_prices") == tomorrow.isoformat():
            return
        t0, t1 = day_range(tomorrow)
        pm = self.app.price_model(now, t1)
        tom = [pm.price(t) for t in range(t0, t1, INTERVAL)]
        if any(p is None for p in tom):
            return  # morgendagens priser er ikke offentliggjort endnu
        hours = cfg["notify_block_hours"]
        blk = calc.cheapest_block(pm, now, t1, hours)
        if not blk:
            return
        when = "i morgen" if local_dt(blk["start"]).date() == tomorrow else "i nat/i dag"
        lines = [f"Billigste {dk(hours, 0)} timer: {when} {hhmm(blk['start'])}–{hhmm(blk['slut'])}"
                 f", gns. {dk(blk['gns_kr_pr_kwh'])} kr/kWh",
                 f"I morgen: lavest {dk(min(tom))} kr, højest {dk(max(tom))} kr pr. kWh"]
        limit = cfg["notify_price_below"]
        if limit is not None:
            cheap = [{"t": t} for t, p in zip(range(t0, t1, INTERVAL), tom) if p <= limit]
            if cheap:
                from .smart import _periods
                per = ", ".join(f"{hhmm(a)}–{hhmm(b)}" for a, b in merge_periods(cheap))
                lines.append(f"Under {dk(limit)} kr: {per}")
        if self.send("Morgendagens elpriser", "\n".join(lines), ["chart_with_downwards_trend"], 2):
            self.app.db.set_kv("notified_prices", tomorrow.isoformat())

    # ------------------------------------------------------------------- fejl --
    def on_errors(self, now):
        if not self.app.cfg["notify_errors"] or not self.n:
            return
        for key, since in list(self.app.error_since.items()):
            if now - since >= ERROR_AFTER and key not in self.notified_errors:
                name = ERROR_NAMES.get(key, key)
                if self.send(f"Problem: {name}",
                             f"{name} har fejlet i over 2 timer:\n{self.app.errors.get(key, '')}",
                             ["warning"], 4):
                    self.notified_errors.add(key)
        for key in list(self.notified_errors):
            if key not in self.app.error_since:
                self.notified_errors.discard(key)
                self.send(f"{ERROR_NAMES.get(key, key)} virker igen", "Alt kører normalt igen.",
                          ["white_check_mark"], 2)

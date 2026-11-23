"""Smart opladning: lad i de billigste kvarterer inden et ønsket klokkeslæt.

Appen lægger en plan ud fra elpriserne. Er Tesla Fleet API sat op, starter og
stopper appen selv opladningen efter planen; ellers vises planen som anbefaling.

Tilstande:  off   – appen rører ikke bilen
            smart – lad i planens kvarterer
            now   – lad med det samme
"""
import math
import re
import time
from datetime import timedelta

from .tesla import VehicleAsleep
from .timeutil import INTERVAL, local_dt
from .wallconnector import power_w

MODES = ("off", "smart", "now")
SOC_REFRESH = 1800  # hent batteriniveau højst hver halve time (Fleet API koster pr. kald)
CMD_REPEAT = 600    # samme kommando gentages tidligst efter 10 min
CMD_MIN_GAP = 60    # mindst 1 min mellem to kommandoer


def parse_hhmm(s):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(s).strip())
    if not m:
        raise ValueError(f"Ugyldigt klokkeslæt {s!r} – brug TT:MM, fx 07:00")
    return int(m.group(1)), int(m.group(2))


def next_deadline(now, hhmm):
    """Næste gang klokken (dansk tid) er hhmm efter tidspunktet now."""
    h, m = parse_hhmm(hhmm)
    d = local_dt(now)
    target = d.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.timestamp() <= now:
        target = (d + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
    return int(target.timestamp())


def merge_periods(slots):
    out = []
    for s in slots:
        if out and out[-1][1] == s["t"]:
            out[-1][1] = s["t"] + INTERVAL
        else:
            out.append([s["t"], s["t"] + INTERVAL])
    return out


def _cost(slots, kwh, per_slot):
    rest, total = kwh, 0.0
    for s in slots:
        if rest <= 1e-9:
            break
        if s["pris"] is None:
            return None
        take = min(per_slot, rest)
        total += take * s["pris"]
        rest -= take
    return round(total, 2)


def make_plan(pm, now, deadline, kwh, charge_kw):
    """Vælg de billigste kvarterer i [now, deadline) til at lade `kwh` kWh.

    Kvarterer uden kendt pris (morgendagens priser offentliggøres ca. kl. 13)
    skønnes ud fra samme kvarter dagen før."""
    plan = {"kwh": round(max(kwh, 0.0), 2), "klar": deadline, "slots": [], "perioder": [],
            "kr": 0.0, "kr_hvis_nu": 0.0, "faerdig_kl": None, "estimeret": False,
            "naar_ikke": False, "faerdig": kwh <= 0.05}
    if plan["faerdig"] or deadline <= now:
        return plan
    per_slot = charge_kw * INTERVAL / 3600
    n = math.ceil(kwh / per_slot - 1e-9)
    cand = []
    for t in range(now - now % INTERVAL, deadline, INTERVAL):
        p, est = pm.price(t), False
        if p is None:
            p, est = pm.price(t - 86400), True
        cand.append({"t": t, "pris": None if p is None else round(p, 4), "estimeret": est})
    chosen = sorted(cand, key=lambda c: (math.inf if c["pris"] is None else c["pris"], c["t"]))[:n]
    chosen.sort(key=lambda c: c["t"])
    plan.update({
        "slots": chosen,
        "perioder": merge_periods(chosen),
        "kr": _cost(chosen, kwh, per_slot),
        "kr_hvis_nu": _cost(cand[:n], kwh, per_slot),
        "faerdig_kl": chosen[-1]["t"] + INTERVAL if chosen else None,
        "estimeret": any(c["estimeret"] for c in chosen),
        "naar_ikke": len(cand) < n,
    })
    return plan


class SmartCharger:
    def __init__(self, cfg, store, car, price_model, clock=time.time):
        self.cfg = cfg
        self.store = store
        self.car = car
        self.price_model = price_model
        self.clock = clock
        self.last_cmd = None
        self.last_cmd_ts = 0
        self.error = None
        self.log = []
        self._reset()

    def _reset(self):
        self.plugged = False
        self.plug_ts = None
        self.soc = None
        self.soc_try_ts = 0
        self.car_full = False
        self.plan = None
        self.remaining = None
        self.source = None
        self.desired = None
        self.charging = False

    # ---------------------------------------------------------- indstillinger --
    def settings(self):
        s = {"mode": "off", "klar_kl": self.cfg["smart_default_ready"],
             "kwh": float(self.cfg["smart_default_kwh"])}
        s.update(self.store.get_kv("smart_settings", {}) or {})
        return s

    def set_settings(self, data):
        s = self.settings()
        if "mode" in data:
            if data["mode"] not in MODES:
                raise ValueError(f"Ukendt tilstand {data['mode']!r}")
            if data["mode"] != s["mode"]:
                self.last_cmd, self.last_cmd_ts = None, 0  # må handle med det samme
            s["mode"] = data["mode"]
        if "klar_kl" in data:
            s["klar_kl"] = "%02d:%02d" % parse_hhmm(data["klar_kl"])
        if "kwh" in data:
            try:
                kwh = float(data["kwh"])
            except (TypeError, ValueError):
                raise ValueError("kWh skal være et tal") from None
            if not 0 < kwh <= 150:
                raise ValueError("kWh skal ligge mellem 1 og 150")
            s["kwh"] = kwh
        self.store.set_kv("smart_settings", s)
        return s

    def _log(self, now, text):
        self.log.insert(0, {"t": int(now), "tekst": text})
        del self.log[12:]

    # ------------------------------------------------------------- styring --
    def update(self, v, now=None):
        """Kaldes ved hver aflæsning af Wall Connectoren (v=None: ukendt status)."""
        now = int(now if now is not None else self.clock())
        if v is None:
            return
        if not v.get("vehicle_connected"):
            if self.plugged:
                self._log(now, "Bilen er koblet fra")
            self._reset()
            return
        if not self.plugged:
            self.plugged, self.plug_ts = True, now
            self._log(now, "Bilen er sat til")
        s = self.settings()
        session_kwh = float(v.get("session_energy_wh") or 0) / 1000
        self.charging = bool(v.get("contactor_closed")) and power_w(v) > 100
        if s["mode"] == "smart" and self.car and now - self.soc_try_ts >= SOC_REFRESH:
            self._refresh_soc(now, session_kwh)
        self.remaining, self.source = self._remaining(s, session_kwh)
        deadline = next_deadline(now, s["klar_kl"])
        pm = self.price_model(now - 86400, deadline)
        self.plan = make_plan(pm, now, deadline, self.remaining, float(self.cfg["charge_kw"]))

        want = None
        if s["mode"] == "now":
            want = not self.car_full
        elif s["mode"] == "smart":
            cur = now - now % INTERVAL
            want = (self.remaining > 0.05 and not self.car_full
                    and any(sl["t"] == cur for sl in self.plan["slots"]))
        self.desired = want
        if want is not None and self.car:
            self._enforce(want, now)

    def _refresh_soc(self, now, session_kwh):
        self.soc_try_ts = now
        try:
            cs = self.car.charge_state()
            self.soc = {"niveau": float(cs["battery_level"]),
                        "graense": float(cs["charge_limit_soc"]),
                        "ts": now, "session_kwh": session_kwh}
            self.error = None
        except VehicleAsleep:
            pass  # bilen sover – brug kWh-målet, prøv igen senere
        except Exception as e:
            self.error = f"Tesla: {e}"

    def _remaining(self, s, session_kwh):
        if self.car_full:
            return 0.0, "fuld"
        if self.soc:
            pct = max(self.soc["graense"] - self.soc["niveau"], 0.0)
            need = pct / 100 * float(self.cfg["battery_kwh"]) / float(self.cfg["charge_efficiency"])
            need -= session_kwh - self.soc["session_kwh"]
            return max(need, 0.0), "soc"
        return max(float(s["kwh"]) - session_kwh, 0.0), "kwh"

    def _enforce(self, want, now):
        if want == self.charging:
            return
        if self.last_cmd == want and now - self.last_cmd_ts < CMD_REPEAT:
            return
        if now - self.last_cmd_ts < CMD_MIN_GAP:
            return
        self.last_cmd, self.last_cmd_ts = want, now
        try:
            reason = self.car.charge_start() if want else self.car.charge_stop()
            if want and reason == "complete":
                self.car_full = True
                self._log(now, "Bilen er fuldt opladet (til ladegrænsen)")
            else:
                self._log(now, "Startede opladning" if want else "Satte opladning på pause")
            self.error = None
        except VehicleAsleep:
            try:
                self.car.wake()
                self._log(now, "Vækker bilen …")
            except Exception as e:
                self.error = f"Tesla: {e}"
            self.last_cmd_ts = now - CMD_REPEAT + 90  # prøv igen om ca. 1½ min
        except Exception as e:
            self.error = f"Tesla: {e}"
            self._log(now, f"Kommando fejlede: {e}")

    # ---------------------------------------------------------------- API --
    def state(self, now=None):
        now = int(now if now is not None else self.clock())
        s = self.settings()
        plan, preview = self.plan, False
        if not self.plugged:
            # Forhåndsvisning: hvad sker der, hvis bilen sættes til nu?
            deadline = next_deadline(now, s["klar_kl"])
            plan = make_plan(self.price_model(now - 86400, deadline), now, deadline,
                             float(s["kwh"]), float(self.cfg["charge_kw"]))
            preview = True
        return {
            "indstillinger": s,
            "tilsluttet": self.plugged,
            "lader": self.charging,
            "plan": plan,
            "forhaandsvisning": preview,
            "mangler_kwh": None if self.remaining is None else round(self.remaining, 2),
            "kilde": self.source,
            "soc": self.soc,
            "styrer_bilen": self.car is not None,
            "oensket": self.desired,
            "fejl": self.error,
            "log": self.log,
            "ladeeffekt_kw": float(self.cfg["charge_kw"]),
        }

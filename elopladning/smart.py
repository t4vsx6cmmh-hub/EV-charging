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
from datetime import datetime, timedelta

from . import autopilot
from .tesla import VehicleAsleep
from .timeutil import DK, INTERVAL, local_dt
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
    def __init__(self, cfg, store, car, price_model, clock=time.time, learner=None):
        self.cfg = cfg
        self.learner = learner      # now -> autopilot-model (eller None)
        self._model, self._model_ts = None, 0
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
        self.session_kwh = 0.0

    # ---------------------------------------------------------- indstillinger --
    def settings(self):
        s = {"mode": "off", "klar_kl": self.cfg["smart_default_ready"],
             "kwh": float(self.cfg["smart_default_kwh"]), "maal_pct": None, "autopilot": True}
        s.update(self.store.get_kv("smart_settings", {}) or {})
        return s

    def model(self, now):
        """Autopilotens model over afgangstider (genberegnes højst én gang i timen)."""
        if not self.learner:
            return None
        if self._model is None or now - self._model_ts > 3600:
            self._model, self._model_ts = self.learner(now), now
        return self._model

    def effective(self, now):
        """De indstillinger, der gælder for næste afgang: engangsændring >
        autopilot > manuel "klar kl."."""
        s = self.settings()
        eff = dict(s)
        model = self.model(now) if s.get("autopilot") else None
        if model:
            t, hhmm, info, day = autopilot.next_departure(model, now, s["klar_kl"])
            eff.update(klar_kl=hhmm, kilde="autopilot", dato=day.isoformat(),
                       forklaring=autopilot.explain(info, day, autopilot.local_dt(now).date()))
            if model.get("kwh") and info is not None:
                eff["kwh"] = eff["kwh_laert"] = float(model["kwh"])
        else:
            t = next_deadline(now, s["klar_kl"])
            eff.update(kilde="manuel", dato=autopilot.local_dt(t).date().isoformat(), forklaring=None)
        ov = self.store.get_kv("smart_override")
        if ov and ov.get("dato") == eff["dato"]:
            for k in ("klar_kl", "maal_pct", "kwh"):
                if ov.get(k) is not None:
                    eff[k] = ov[k]
            if ov.get("klar_kl"):
                day = datetime.strptime(ov["dato"], "%Y-%m-%d").date()
                h, m = parse_hhmm(ov["klar_kl"])
                t = int(datetime(day.year, day.month, day.day, h, m, tzinfo=DK).timestamp())
                if t <= now:
                    t = next_deadline(now, ov["klar_kl"])
            eff["kilde"] = "engang"
            eff["engang"] = ov
        elif ov and ov.get("dato", "") < autopilot.local_dt(now).date().isoformat():
            self.store.set_kv("smart_override", None)  # udløbet
        eff["deadline"] = t
        return eff

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
        if "maal_pct" in data:
            v = data["maal_pct"]
            if v is not None:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise ValueError("Mål i % skal være et tal") from None
                if not 20 <= v <= 100:
                    raise ValueError("Mål i % skal ligge mellem 20 og 100")
            s["maal_pct"] = v
        if "autopilot" in data:
            s["autopilot"] = bool(data["autopilot"])
        if "engang" in data:
            self._set_override(data["engang"])
        self.store.set_kv("smart_settings", s)
        return s

    def _set_override(self, ov):
        """Engangsændring for næste afgang, fx {"maal_pct": 100} til en lang tur."""
        if not ov:
            self.store.set_kv("smart_override", None)
            return
        if not isinstance(ov, dict):
            raise ValueError("engang skal være et objekt")
        clean = {}
        if ov.get("klar_kl"):
            clean["klar_kl"] = "%02d:%02d" % parse_hhmm(ov["klar_kl"])
        if ov.get("maal_pct") is not None:
            v = float(ov["maal_pct"])
            if not 20 <= v <= 100:
                raise ValueError("Mål i % skal ligge mellem 20 og 100")
            clean["maal_pct"] = v
        if ov.get("kwh") is not None:
            v = float(ov["kwh"])
            if not 0 < v <= 150:
                raise ValueError("kWh skal ligge mellem 1 og 150")
            clean["kwh"] = v
        now = int(self.clock())
        self.store.set_kv("smart_override", None)  # find næste afgang uden den gamle ændring
        clean["dato"] = self.effective(now)["dato"]
        self.store.set_kv("smart_override", clean)

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
                self._record(now, "out", self.session_kwh)
            self._reset()
            return
        if not self.plugged:
            self.plugged, self.plug_ts = True, now
            self._log(now, "Bilen er sat til")
            self._record(now, "in", None)
        s = self.effective(now)
        session_kwh = float(v.get("session_energy_wh") or 0) / 1000
        self.session_kwh = session_kwh
        self.charging = bool(v.get("contactor_closed")) and power_w(v) > 100
        if self.car and now - self.soc_try_ts >= SOC_REFRESH:
            self._refresh_soc(now, session_kwh)
        self.remaining, self.source = self._remaining(s, session_kwh)
        deadline = s["deadline"]
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

    def _record(self, now, kind, kwh):
        """Gem tilslutning/afgang – autopiloten lærer af dem."""
        if hasattr(self.store, "add_plug_event"):
            self.store.add_plug_event(now, kind, kwh)
            self._model = None

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
            pct = max(self.target_pct(s) - self.soc["niveau"], 0.0)
            need = pct / 100 * float(self.cfg["battery_kwh"]) / float(self.cfg["charge_efficiency"])
            need -= session_kwh - self.soc["session_kwh"]
            return max(need, 0.0), "soc"
        return max(float(s["kwh"]) - session_kwh, 0.0), "kwh"

    def target_pct(self, s=None):
        """Mål i %: eget mål fra appen, dog højst bilens ladegrænse."""
        s = s or self.effective(int(self.clock()))
        limit = self.soc["graense"] if self.soc else 100.0
        return min(float(s["maal_pct"]), limit) if s.get("maal_pct") else limit

    def level_now(self):
        """Skønnet batteriniveau nu: sidste aflæsning + det ladet siden."""
        if not self.soc:
            return None
        added = (self.session_kwh - self.soc["session_kwh"]) * float(self.cfg["charge_efficiency"])
        return round(min(self.soc["niveau"] + added / float(self.cfg["battery_kwh"]) * 100, 100.0), 1)

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
        s = self.effective(now)
        plan, preview = self.plan, False
        if not self.plugged:
            # Forhåndsvisning: hvad sker der, hvis bilen sættes til nu?
            deadline = s["deadline"]
            plan = make_plan(self.price_model(now - 86400, deadline), now, deadline,
                             float(s["kwh"]), float(self.cfg["charge_kw"]))
            preview = True
        model = self.model(now) if s.get("autopilot") else None
        return {
            "indstillinger": self.settings(),
            "naeste": {k: s.get(k) for k in ("deadline", "klar_kl", "kilde", "forklaring", "dato",
                                              "maal_pct", "kwh", "engang", "kwh_laert")},
            "autopilot": model,
            "tilsluttet": self.plugged,
            "lader": self.charging,
            "plan": plan,
            "forhaandsvisning": preview,
            "mangler_kwh": None if self.remaining is None else round(self.remaining, 2),
            "kilde": self.source,
            "soc": self.soc,
            "niveau_nu": self.level_now(),
            "maal_pct": self.target_pct(s) if self.soc else s.get("maal_pct"),
            "session_kwh": round(self.session_kwh, 2),
            "styrer_bilen": self.car is not None,
            "oensket": self.desired,
            "fejl": self.error,
            "log": self.log,
            "ladeeffekt_kw": float(self.cfg["charge_kw"]),
        }

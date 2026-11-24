"""
Journal de course → debug.log (source "race").

Pour chaque course : un bloc RACE START (config du code + état de la voiture), une ligne
par trait mesuré, chaque consigne de vitesse/feux envoyée, les changements d'état de la
voiture (onTrack, batterie, vitesse mesurée, rssi…), les conflits d'intention d'autres
appareils sur le broker, puis un bloc RACE STOP avec un résumé.

Filtrer le log :  source == "race"  (champ "raceId" pour séparer les courses).
Tous les horaires sont en heure locale ISO + "t_ms" depuis le départ.
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import debug_log

_SRC = "race"
_ROOT = Path(__file__).resolve().parent

# Champs d'état voiture suivis (log seulement si changement)
_CAR_FIELDS = ("status", "connected", "onTrack", "batteryLevel", "charging", "speed", "rssi")


def _git_rev() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return out.strip() or None
    except Exception:
        return None


_CODE_REV = _git_rev()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="milliseconds")


class RaceJournal:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._run: dict[str, Any] | None = None

    # ——— helpers ———
    def _log(self, level: str, message: str, **data: Any) -> None:
        run = self._run
        if run is not None:
            data.setdefault("raceId", run["id"])
            data.setdefault("t_ms", int((time.time() - run["t0"]) * 1000))
        debug_log.write(level, _SRC, message, **data)

    @staticmethod
    def _car_state(orch: Any, vehicle_id: str | None) -> dict[str, Any] | None:
        if not vehicle_id:
            return None
        # Pas de verrou ici : le journal est appelé depuis _emit, parfois sous le verrou du bridge
        # (risque d'interblocage). Une copie superficielle suffit, on réessaie si le dict bouge.
        v: dict[str, Any] = {}
        for _ in range(3):
            try:
                v = dict(orch._vehicles.get(vehicle_id) or orch._vehicles.get(vehicle_id.upper()) or {})
                break
            except Exception:
                continue
        if not v:
            return None
        out = {k: v.get(k) for k in _CAR_FIELDS if k in v}
        last = v.get("lastSeen")
        if last:
            out["lastSeenAgoS"] = round(time.time() - float(last), 2)
        if v.get("wheelDistance"):
            out["lastWheelDistance"] = v["wheelDistance"]
        if v.get("model"):
            out["model"] = v["model"]
        return out

    @staticmethod
    def _code_state(orch: Any, ev: dict[str, Any]) -> dict[str, Any]:
        tr = orch.track
        segs = ((tr.map or {}).get("segments")) or []
        info: dict[str, Any] = {
            "codeRev": _CODE_REV,
            "callerId": getattr(orch, "caller_id", None),
            "broker": f"{getattr(orch, 'host', '?')}:{getattr(orch, 'port', '?')}",
            "mqttConnected": getattr(orch, "_connected", None),
            "holdEnabled": dict(getattr(orch, "_hold_enabled", {}) or {}),
            "holdHz": getattr(orch, "_hold_hz", None),
            "course": getattr(tr, "course_name", None),
            "segmentCount": len(segs),
            "partCount": getattr(tr, "part_count", None),
            "startSegment": ev.get("currentSegment"),
            "straightMaxSpeed": getattr(tr, "straight_max_speed", None),
            "turnSpeed": getattr(tr, "turn_speed", None),
            "preturnSpeed": getattr(tr, "preturn_speed", None),
            "lookaheadSegments": getattr(tr, "lookahead_segments", None),
            "brakeStrengthPct": getattr(tr, "brake_strength_pct", None),
        }
        try:
            prof = tr.race_profile()
            info["profile"] = "".join(
                f"{p['index']}:{(p['kind'] or '?')[0]}{p['speed']} " for p in prof
            ).strip()
        except Exception as exc:
            info["profileError"] = str(exc)
        try:
            info["segments"] = " ".join(
                f"{i}:{round(float(s.get('left', 0)))}/{round(float(s.get('right', 0)))}"
                for i, s in enumerate(segs)
            )
        except Exception:
            pass
        return info

    # ——— entrée unique : tous les événements du bridge ———
    def on_event(self, event: dict[str, Any], orch: Any) -> None:
        try:
            with self._lock:
                self._handle(event, orch)
        except Exception as exc:  # le journal ne doit jamais gêner la course
            debug_log.warn(_SRC, "journal error", error=f"{type(exc).__name__}: {exc}")

    def _handle(self, ev: dict[str, Any], orch: Any) -> None:
        et = ev.get("type")

        if et == "raceStarted":
            if self._run is not None:
                self._finish("restarted", orch)
            vid = ev.get("vehicleId")
            now = time.time()
            self._run = {
                "id": "R" + datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S"),
                "t0": now,
                "vid": str(vid or ""),
                "traits": 0,
                "matched": 0,
                "unmatched": 0,
                "big": 0,
                "short": 0,
                "first_unmatched": None,
                "speed_cmds": 0,
                "speed_refresh": 0,
                "lights_cmds": 0,
                "conflicts": 0,
                "car_changes": 0,
                "last_trait_ts": now,
                "last_speed": None,
                "car": self._car_state(orch, vid),
                "n": None,
                "start_seg": ev.get("currentSegment"),
                "last_seg": ev.get("currentSegment"),
                "dts": [],
            }
            code = self._code_state(orch, ev)
            self._run["n"] = code.get("segmentCount")
            self._log(
                "INFO",
                "RACE START",
                startedAt=_iso(now),
                vehicleId=vid,
                firstTargetSpeed=ev.get("targetSpeed"),
                firstPlan=ev.get("plan"),
                car=self._run["car"],
                code=code,
            )
            return

        run = self._run
        if run is None:
            if et == "intentConflict":
                debug_log.warn(_SRC, "intentConflict (hors course)", **{k: v for k, v in ev.items() if k != "type"})
            return

        vid = run["vid"].upper()

        if et == "mapPosition" and str(ev.get("vehicleId") or "").upper() == vid:
            self._trait(ev, run, orch)
        elif et == "mqttOut":
            self._mqtt_out(ev, run, vid)
        elif et == "vehicleUpdate" and str(ev.get("vehicleId") or "").upper() == vid:
            self._car_update(ev, run)
        elif et == "intentConflict":
            run["conflicts"] += 1
            self._log("WARN", "CONFLIT D'INTENTION (autre appareil sur le broker)", **{k: v for k, v in ev.items() if k != "type"})
        elif et in ("mqttDisconnected", "mqttError"):
            self._log("WARN", f"broker: {et}", detail={k: v for k, v in ev.items() if k != "type"})
        elif et == "mqttConnected":
            self._log("INFO", "broker: mqttConnected")
        elif et == "holdChanged":
            self._log("INFO", "hold changé", detail={k: v for k, v in ev.items() if k != "type"})
        elif et == "raceStopped":
            self._finish("stop (bouton Stop / API)", orch)
        elif et == "manualOverride" and "race" in (ev.get("stopped") or []):
            self._finish("manualOverride (cockpit a repris la main)", orch)

    # ——— détails ———
    def _trait(self, ev: dict[str, Any], run: dict[str, Any], orch: Any) -> None:
        now = time.time()
        dt = now - run["last_trait_ts"]
        run["last_trait_ts"] = now
        run["traits"] += 1
        s = ev.get("sample") or {}
        x = ev.get("expected") or {}
        sl, sr = float(s.get("left") or 0), float(s.get("right") or 0)
        xl, xr = float(x.get("left") or 0), float(x.get("right") or 0)
        exp_sum = xl + xr
        ratio = round((sl + sr) / exp_sum, 2) if exp_sum > 0 else None
        matched = bool(ev.get("matched"))
        first = run["traits"] == 1  # 1er trait = départ en cours de bout, jamais comparable
        flag = None
        if not first:
            if matched:
                run["matched"] += 1
            else:
                run["unmatched"] += 1
                if run["first_unmatched"] is None:
                    run["first_unmatched"] = run["traits"]
            if ratio is not None:
                if ratio >= 1.8:
                    run["big"] += 1
                    flag = "TRAIT_PERDU?(mesure >= 1.8x attendu)"
                elif ratio <= 0.55:
                    run["short"] += 1
                    flag = "TRAIT_PARASITE?(mesure <= 0.55x attendu)"
        run["dts"].append(dt)
        run["last_seg"] = ev.get("currentSegment")
        plan = ev.get("plan") or {}
        n = run.get("n") or ev.get("partCount") or 0
        lap = None
        if n:
            lap = round((int(run["start_seg"] or 0) + run["traits"] - 1) / n, 2)
        self._log(
            "WARN" if flag else "INFO",
            "trait",
            n=run["traits"],
            dt_s=round(dt, 3),
            finished=ev.get("finishedSegment"),
            next=ev.get("currentSegment"),
            sampleLR=[round(sl, 1), round(sr, 1)],
            expectedLR=[round(xl, 1), round(xr, 1)],
            ratio=ratio,
            matched=None if first else matched,
            firstPartial=first or None,
            lapEstimate=lap,
            nextKind=plan.get("kind"),
            commandedSpeed=ev.get("targetSpeed"),
            flag=flag,
            car=self._car_state(orch, run["vid"]),
        )

    def _mqtt_out(self, ev: dict[str, Any], run: dict[str, Any], vid_u: str) -> None:
        topic = str(ev.get("topic") or "")
        parts = topic.split("/")
        # Anki/Vehicles/U/{vid}/I/{caller}
        if len(parts) < 6 or parts[1] != "Vehicles" or parts[3].upper() != vid_u:
            return
        payload = ev.get("payload") or {}
        t = payload.get("type")
        pl = payload.get("payload") or {}
        if t == "speed":
            key = (pl.get("velocity"), pl.get("acceleration"))
            if key == run["last_speed"]:
                run["speed_refresh"] += 1  # renvoi Hold identique : on compte, on ne logue pas
                return
            prev = run["last_speed"]
            run["last_speed"] = key
            run["speed_cmds"] += 1
            self._log(
                "INFO",
                "CMD vitesse",
                velocity=key[0],
                acceleration=key[1],
                previous=None if prev is None else {"velocity": prev[0], "acceleration": prev[1]},
                segmentNow=run.get("last_seg"),
            )
        elif t == "lights":
            run["lights_cmds"] += 1
            effects = {k: (v or {}).get("effect") for k, v in pl.items() if isinstance(v, dict)}
            self._log("INFO", "CMD feux", effects=effects)
        elif t:
            self._log("WARN", f"CMD {t} (inattendu en course)", payload=pl)

    def _car_update(self, ev: dict[str, Any], run: dict[str, Any]) -> None:
        state = ev.get("state") or {}
        cur = {k: state.get(k) for k in _CAR_FIELDS if k in state}
        prev = run.get("car_live") or run.get("car") or {}
        changed = {k: v for k, v in cur.items() if prev.get(k) != v}
        run["car_live"] = {**prev, **cur}
        if not changed:
            return
        # rssi bruité : on ne le logue que s'il bouge de >= 6 dB
        if set(changed) == {"rssi"} and prev.get("rssi") is not None:
            try:
                if abs(int(changed["rssi"]) - int(prev["rssi"])) < 6:
                    run["car_live"]["rssi"] = prev["rssi"]
                    return
            except Exception:
                pass
        run["car_changes"] += 1
        lvl = "WARN" if ("onTrack" in changed and changed["onTrack"] is False) or (
            "connected" in changed and changed["connected"] is False
        ) else "INFO"
        self._log(lvl, "CAR état", changed=changed, previous={k: prev.get(k) for k in changed})

    def _finish(self, reason: str, orch: Any) -> None:
        run = self._run
        if run is None:
            return
        now = time.time()
        dur = now - run["t0"]
        cmp_n = run["matched"] + run["unmatched"]
        dts = run["dts"][1:] if len(run["dts"]) > 1 else run["dts"]
        self._log(
            "INFO",
            "RACE STOP",
            stoppedAt=_iso(now),
            reason=reason,
            durationS=round(dur, 2),
            traits=run["traits"],
            comparable=cmp_n,
            matched=run["matched"],
            unmatched=run["unmatched"],
            matchedPct=round(100 * run["matched"] / cmp_n) if cmp_n else None,
            firstUnmatchedTrait=run["first_unmatched"],
            traitsLostSuspected=run["big"],
            traitsParasiteSuspected=run["short"],
            startSegment=run["start_seg"],
            lastSegment=run["last_seg"],
            speedCommands=run["speed_cmds"],
            holdSpeedRefreshesSkipped=run["speed_refresh"],
            lightsCommands=run["lights_cmds"],
            intentConflicts=run["conflicts"],
            carStateChanges=run["car_changes"],
            meanTraitIntervalS=round(sum(dts) / len(dts), 3) if dts else None,
            carAtStart=run["car"],
            carAtEnd=self._car_state(orch, run["vid"]),
        )
        self._run = None


journal = RaceJournal()

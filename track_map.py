"""
Track mapping from Anki wheelDistance traits.

wheelDistance value: { left, right } — distance since previous trait.
right < left → turn toward the right (right wheel travelled less).
First sample after starting mapping is discarded (often MQTT retained).

Mapping = 2 tours complets enregistrés (rien n’est ignoré au tour 2) :
  Tour 1 → bouts numérotés 0..N-1
  Tour 2 → bouts numérotés 0..N-1 (toutes les mesures gardées)
  Puis comparaison bout à bout + fusion (moyenne) pour finaliser la map.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[dict[str, Any]], None]

# Voie / essieu effectif (plus petit = virages plus marqués sur le dessin).
_AXLE = 22.0
# Amplifie la courbure selon l'écart L/R des roues (évite les "droites presque plates").
_CURVE_GAIN = 3.2
# Mapping lap match: ~10%.
_MATCH_RATIO = 1.1
# Course: un bout peut varier jusqu'à ~30% et rester reconnu (ex. 1.3/1).
_RACE_MATCH_RATIO = 1.3
# Floor so tiny distances still match without exploding the ratio.
_MATCH_ABS_FLOOR = 3.0
_MIN_SEGMENTS = 3
# Course auto : droits réglables, virages selon curseur de ralentissement.
_STRAIGHT_MAX_SPEED = 1000
_CURVE_SPEED_AT_0 = 500  # 0% ralentissement → 500 dans les virages ; 100% → 0
# Anticipation : regarder N bouts suivants pour la cible de freinage.
_DEFAULT_LOOKAHEAD = 1
# Freinage à une fraction du droit (temps estimé / mesuré entre traits).
_DEFAULT_BRAKE_AT = 0.75  # 3/4 du chemin (ancien frein mi-bout, non utilisé)
# Freinage avant virage : 100 % = vitesse du virage dès l'entrée du droit précédent.
_DEFAULT_BRAKE_STRENGTH = 100.0
# Conversion empirique longueur_map → temps : length / (speed * scale) secondes.
_DIST_PER_SPEED_PER_SEC = 0.07
_MAPS_DIR = Path(__file__).resolve().parent / "maps"
_MAP_PATH = _MAPS_DIR / "track.json"
_COURSES_DIR = _MAPS_DIR / "courses"


def _safe_course_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", (name or "").strip(), flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValueError("Nom de course invalide")
    return cleaned[:64]


def classify_turn(left: float, right: float) -> str:
    if right < left - 1:
        return "right"
    if left < right - 1:
        return "left"
    return "straight"


def segment_length(left: float, right: float) -> float:
    return max(0.0, (float(left) + float(right)) / 2.0)


def heading_delta(left: float, right: float, gain: float = _CURVE_GAIN) -> float:
    """Positive = turn right (clockwise if +Y forward).

    Agrandit la courbe selon l'écart de distance des roues (tours L/R),
    pas seulement un petit (L-R)/essieu qui donnait des bouts quasi droits.
    """
    left_f = float(left)
    right_f = float(right)
    diff = left_f - right_f
    avg = max((left_f + right_f) / 2.0, 1.0)
    slip = abs(diff) / avg  # 0 = droit, ~0.3+ = virage net
    # Base différentielle + gain + boost si les roues divergent beaucoup
    delta = (diff / _AXLE) * float(gain) * (1.0 + 1.25 * slip)
    # Évite les spins irréalistes sur un seul bout
    return max(-math.pi * 0.95, min(math.pi * 0.95, delta))


def _scale_headings_to_loop(deltas: list[float]) -> list[float]:
    """Force exactement un tour (±2π) : circuit simple, un seul chemin sans croisement."""
    if not deltas:
        return deltas
    total = sum(deltas)
    if abs(total) < 1e-6:
        # Pas de virage mesuré : petite rotation uniforme pour pouvoir fermer
        n = len(deltas)
        return [2 * math.pi / n] * n
    target = math.copysign(2 * math.pi, total)
    scale = target / total
    scale = max(0.35, min(3.5, scale))
    return [d * scale for d in deltas]


def _seg_intersect(
    a: dict[str, float],
    b: dict[str, float],
    c: dict[str, float],
    d: dict[str, float],
) -> bool:
    """Intersection propre (pas seulement un sommet partagé)."""
    ax, ay, bx, by = a["x"], a["y"], b["x"], b["y"]
    cx, cy, dx, dy = c["x"], c["y"], d["x"], d["y"]

    def cross(ox: float, oy: float, px: float, py: float, qx: float, qy: float) -> float:
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = cross(cx, cy, dx, dy, ax, ay)
    d2 = cross(cx, cy, dx, dy, bx, by)
    d3 = cross(ax, ay, bx, by, cx, cy)
    d4 = cross(ax, ay, bx, by, dx, dy)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def _path_self_intersects(path: list[dict[str, float]]) -> bool:
    """True si le tracé se croise (interdit : un seul chemin bout à bout)."""
    if len(path) < 4:
        return False
    pts = path[:-1] if (abs(path[0]["x"] - path[-1]["x"]) < 1e-6 and abs(path[0]["y"] - path[-1]["y"]) < 1e-6) else path
    n = len(pts)
    if n < 4:
        return False
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            # ignorer arêtes adjacentes / partageant un sommet
            if abs(i - j) <= 1 or (i == 0 and j == n - 1):
                continue
            if _seg_intersect(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return True
    return False


def _path_area(path: list[dict[str, float]]) -> float:
    pts = path[:-1] if path and abs(path[0]["x"] - path[-1]["x"]) < 1e-6 and abs(path[0]["y"] - path[-1]["y"]) < 1e-6 else path
    if len(pts) < 3:
        return 0.0
    area = 0.0
    for i in range(len(pts)):
        j = (i + 1) % len(pts)
        area += pts[i]["x"] * pts[j]["y"] - pts[j]["x"] * pts[i]["y"]
    return abs(area) * 0.5


def _open_path_gap(path: list[dict[str, float]]) -> float:
    if len(path) < 2:
        return 0.0
    dx = path[0]["x"] - path[-1]["x"]
    dy = path[0]["y"] - path[-1]["y"]
    return math.hypot(dx, dy)


def _seed_curve_gain(segments: list[dict[str, Any]], part_count: int) -> float:
    """Heuristique initiale selon nb de bouts et écarts L/R moyens."""
    n = max(1, int(part_count) or len(segments) or 1)
    slips: list[float] = []
    for s in segments:
        left = float(s["left"])
        right = float(s["right"])
        avg = max((left + right) / 2.0, 1.0)
        slips.append(abs(left - right) / avg)
    mean_slip = sum(slips) / max(len(slips), 1)
    # Peu de bouts / gros écarts → gain plus bas ; beaucoup de bouts / petits écarts → gain plus haut
    gain = (2.4 / max(mean_slip, 0.08)) * (8.0 / max(n, 1)) ** 0.35
    return max(0.7, min(5.5, gain))


def optimize_curve_gain(
    segments: list[dict[str, Any]],
    part_count: int | None = None,
) -> tuple[float, dict[str, Any]]:
    """Choisit le gain qui ferme un circuit simple sans croisement."""
    n = int(part_count or len(segments) or 1)
    seed = _seed_curve_gain(segments, n)
    candidates = []
    g = 0.6
    while g <= 5.8:
        candidates.append(round(g, 2))
        g += 0.12
    # densifier autour du seed
    for dg in (-0.4, -0.2, 0.0, 0.2, 0.4, 0.6):
        candidates.append(round(max(0.6, min(5.8, seed + dg)), 2))
    candidates = sorted(set(candidates))

    best_gain = seed
    best_score = -1e18
    best_meta: dict[str, Any] = {"crossed": True}
    for gain in candidates:
        _segs, path = build_geometry(segments, curve_gain=gain, close_loop=False)
        gap = _open_path_gap(path)
        closed = _close_loop_path(list(path))
        if closed:
            closed = closed + [dict(closed[0])]
        crossed = _path_self_intersects(closed)
        area = _path_area(closed)
        # Score : pas de croisement obligatoire, puis petit gap, aire confortable
        score = 0.0
        if crossed:
            score -= 5000.0
        else:
            score += 2000.0
        score -= gap * 4.0
        score += math.sqrt(max(area, 1.0)) * 0.35
        # Pénalise les gains extrêmes loin du seed
        score -= abs(gain - seed) * 15.0
        if score > best_score:
            best_score = score
            best_gain = gain
            best_meta = {
                "crossed": crossed,
                "gap": round(gap, 2),
                "area": round(area, 1),
                "seed": round(seed, 3),
                "partCount": n,
            }
    return best_gain, best_meta


def _value_close(x: float, y: float, ratio: float = _MATCH_RATIO, abs_floor: float = _MATCH_ABS_FLOOR) -> bool:
    ax, ay = abs(float(x)), abs(float(y))
    if abs(ax - ay) <= abs_floor:
        return True
    lo = min(ax, ay)
    hi = max(ax, ay)
    if lo <= 1e-6:
        return hi <= abs_floor
    return (hi / lo) <= ratio


def samples_match(a: dict[str, Any], b: dict[str, Any], ratio: float = _MATCH_RATIO) -> bool:
    """True if left/right are within ~ratio (default 1.1) of each other."""
    return _value_close(a["left"], b["left"], ratio) and _value_close(a["right"], b["right"], ratio)


def sample_confidence(a: dict[str, Any], b: dict[str, Any], ratio: float = _MATCH_RATIO) -> float:
    """Confiance explicite 0..1 entre deux mesures L/R (1 = très proche)."""

    def side(x: float, y: float) -> float:
        ax, ay = abs(float(x)), abs(float(y))
        if abs(ax - ay) <= _MATCH_ABS_FLOOR:
            return 1.0
        lo, hi = min(ax, ay), max(ax, ay)
        if lo <= 1e-6:
            return 1.0 if hi <= _MATCH_ABS_FLOOR else 0.0
        r = hi / lo
        if r <= 1.0:
            return 1.0
        # 1.0 → confiance 1 ; ratio*1.5 → 0
        span = max(0.05, float(ratio) * 1.5 - 1.0)
        return max(0.0, min(1.0, 1.0 - (r - 1.0) / span))

    return round((side(a["left"], b["left"]) + side(a["right"], b["right"])) / 2.0, 3)


def merge_lap_samples(
    lap1: list[dict[str, Any]],
    lap2: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fusionne deux tours numérotés : moyenne L/R, garde les deux mesures + confiance."""
    n = len(lap1)
    if n <= 0:
        return [], {"segmentCount": 0, "matchedCount": 0, "meanConfidence": 0.0}
    merged: list[dict[str, Any]] = []
    matched_count = 0
    conf_sum = 0.0
    for i in range(n):
        a = lap1[i]
        b = lap2[i] if i < len(lap2) else None
        if b is None:
            conf = 0.0
            matched = False
            left = float(a["left"])
            right = float(a["right"])
        else:
            conf = sample_confidence(a, b)
            matched = samples_match(a, b)
            left = (float(a["left"]) + float(b["left"])) / 2.0
            right = (float(a["right"]) + float(b["right"])) / 2.0
            conf_sum += conf
            if matched:
                matched_count += 1
        seg = {
            "id": i,
            "left": round(left, 3),
            "right": round(right, 3),
            "turn": classify_turn(left, right),
            "length": round(segment_length(left, right), 3),
            "maxSpeed": int(a.get("maxSpeed", 700)),
            "lap1": {"left": a["left"], "right": a["right"], "turn": a.get("turn")},
            "lap2": (
                {"left": b["left"], "right": b["right"], "turn": b.get("turn")}
                if b is not None
                else None
            ),
            "matched": matched,
            "confidence": conf,
        }
        merged.append(seg)
    mean_conf = round(conf_sum / n, 3) if n else 0.0
    meta = {
        "segmentCount": n,
        "lap2Count": len(lap2),
        "matchedCount": matched_count,
        "meanConfidence": mean_conf,
        "matchRatio": _MATCH_RATIO,
    }
    return merged, meta


def turn_tightness(left: float, right: float) -> float:
    """0 = droit, 1 = virage très serré (écart L/R relatif)."""
    avg = max((float(left) + float(right)) / 2.0, 1.0)
    slip = abs(float(left) - float(right)) / avg
    return max(0.0, min(1.0, (slip - 0.06) / 0.5))


def race_speed_for_segment(
    seg: dict[str, Any],
    slowdown_pct: float,
    *,
    straight_max: int = _STRAIGHT_MAX_SPEED,
) -> int:
    """Droits → vitesse max réglable. Virages → 500*(1-pct/100), encore plus bas si serré."""
    left = float(seg.get("left", 0))
    right = float(seg.get("right", 0))
    turn = seg.get("turn") or classify_turn(left, right)
    tight = turn_tightness(left, right)

    if turn == "straight" and tight < 0.12:
        return int(max(0, min(1000, straight_max)))

    pct = max(0.0, min(100.0, float(slowdown_pct))) / 100.0
    # 0% → 500 ; 100% → 0
    base = _CURVE_SPEED_AT_0 * (1.0 - pct)
    # Plus le virage est serré, plus on freine (jusqu'à ~75% de baisse supplémentaire)
    speed = base * (1.0 - 0.75 * tight)
    # Contours : plafonnés un peu sous 500 sauf curseur à 0%
    if pct > 0:
        speed = min(speed, 450.0)
    return int(max(0, min(1000, round(speed))))


def _is_straight_seg(seg: dict[str, Any]) -> bool:
    left = float(seg.get("left", 0))
    right = float(seg.get("right", 0))
    turn = seg.get("turn") or classify_turn(left, right)
    return turn == "straight" and turn_tightness(left, right) < 0.12


def _turn_dir(seg: dict[str, Any]) -> str:
    left = float(seg.get("left", 0))
    right = float(seg.get("right", 0))
    return str(seg.get("turn") or classify_turn(left, right))


def preturn_speed(straight_max: int, turn_speed: int, brake_strength_pct: float) -> int:
    """Vitesse du droit juste avant un virage.

    100 % = vitesse du virage (freine à fond dès l'entrée du droit).
    0 % = pas de freinage (reste à la vitesse des droits).
    150 % = moitié de la vitesse du virage (freine plus fort que nécessaire).
    """
    top = int(max(0, min(1000, straight_max)))
    tv = int(turn_speed)
    if top <= tv:
        return top
    pct = max(0.0, min(150.0, float(brake_strength_pct)))
    if pct <= 100.0:
        v = top - (top - tv) * (pct / 100.0)
    else:
        v = tv * (1.0 - (pct - 100.0) / 100.0)
    return int(max(0, min(1000, round(v))))


def build_race_profile(
    segments: list[dict[str, Any]],
    slowdown_pct: float,
    *,
    straight_max: int = _STRAIGHT_MAX_SPEED,
    lookahead: int = _DEFAULT_LOOKAHEAD,
    brake_strength_pct: float = _DEFAULT_BRAKE_STRENGTH,
) -> list[dict[str, Any]]:
    """Profil de vitesse complet, calculé UNE fois avant le départ.

    - Droits : vitesse max.
    - Droit(s) juste avant un virage (jusqu'à `lookahead` bouts) : vitesse du virage
      ajustée par le curseur de freinage → le robot ralentit dès l'entrée du bout.
    - Virages : une vitesse fixe par virage (moyenne de ses bouts), pas d'accélération.
    La piste est une boucle : le dernier bout précède le premier.
    """
    n = len(segments)
    if n <= 0:
        return []
    straight = [_is_straight_seg(s) for s in segments]
    top = int(max(0, min(1000, straight_max)))
    speeds: list[int] = [top] * n
    kinds: list[str] = ["straight"] * n
    run_ids: list[int | None] = [None] * n

    if not any(not s for s in straight):
        return [
            {"index": i, "kind": "straight", "speed": top, "runId": None}
            for i in range(n)
        ]

    dirs = [None if straight[i] else _turn_dir(segments[i]) for i in range(n)]
    # Départ des runs : un bout de virage dont le précédent n'est pas du même virage
    start = None
    for i in range(n):
        prev = (i - 1) % n
        if dirs[i] is not None and dirs[i] != dirs[prev]:
            start = i
            break
    if start is None:
        # Tout est un seul virage dans la même direction
        start = 0

    runs: list[list[int]] = []
    i = 0
    while i < n:
        idx = (start + i) % n
        if dirs[idx] is None:
            i += 1
            continue
        run = [idx]
        j = i + 1
        while j < n and dirs[(start + j) % n] == dirs[idx]:
            run.append((start + j) % n)
            j += 1
        runs.append(run)
        i = j

    run_speed: dict[int, int] = {}
    for rid, run in enumerate(runs):
        per_bout = [
            race_speed_for_segment(segments[k], slowdown_pct, straight_max=top)
            for k in run
        ]
        v = int(round(sum(per_bout) / max(1, len(per_bout))))
        run_speed[rid] = v
        for k in run:
            speeds[k] = v
            kinds[k] = "turn"
            run_ids[k] = rid

    look = max(0, min(3, int(lookahead)))
    if look > 0:
        for rid, run in enumerate(runs):
            first = run[0]
            tv = run_speed[rid]
            pre = preturn_speed(top, tv, brake_strength_pct)
            for step in range(1, look + 1):
                k = (first - step) % n
                if dirs[k] is not None:
                    break  # autre virage juste avant : rien à freiner de plus
                speeds[k] = min(speeds[k], pre)
                kinds[k] = "preturn"
                run_ids[k] = rid

    return [
        {"index": i, "kind": kinds[i], "speed": int(speeds[i]), "runId": run_ids[i]}
        for i in range(n)
    ]


def estimate_travel_ms(length: float, speed: int, measured_ms: float | None = None) -> float:
    """Durée estimée pour parcourir un bout (ms). Préfère le temps mesuré au tour précédent."""
    if measured_ms is not None and measured_ms > 60:
        return float(measured_ms)
    rate = max(float(speed), 1.0) * _DIST_PER_SPEED_PER_SEC
    return 1000.0 * max(float(length), 1.0) / rate


def race_speed_plan(
    segments: list[dict[str, Any]],
    index: int,
    slowdown_pct: float,
    *,
    straight_max: int = _STRAIGHT_MAX_SPEED,
    lookahead: int = _DEFAULT_LOOKAHEAD,
    brake_at_fraction: float = _DEFAULT_BRAKE_AT,
    measured_ms: float | None = None,
    brake_strength_pct: float = _DEFAULT_BRAKE_STRENGTH,
    profile: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan de vitesse d'un bout = simple lecture du profil précalculé.

    Plus de timer de frein mi-parcours : la vitesse est fixée à l'entrée du bout.
    (`brake_at_fraction` / `measured_ms` gardés pour compatibilité, non utilisés.)
    """
    if not segments:
        return {
            "segmentIndex": 0,
            "entrySpeed": 0,
            "brakeSpeed": 0,
            "brakeAfterMs": None,
            "length": 0.0,
            "isStraight": False,
        }
    n = len(segments)
    idx = int(index) % n
    seg = segments[idx]
    if profile is None or len(profile) != n:
        profile = build_race_profile(
            segments,
            slowdown_pct,
            straight_max=straight_max,
            lookahead=lookahead,
            brake_strength_pct=brake_strength_pct,
        )
    entry_info = profile[idx]
    entry = int(entry_info["speed"])
    left = float(seg.get("left", 0))
    right = float(seg.get("right", 0))
    length = segment_length(left, right)
    return {
        "segmentIndex": idx,
        "entrySpeed": entry,
        "brakeSpeed": entry,
        "brakeAfterMs": None,
        "length": round(length, 2),
        "isStraight": entry_info["kind"] != "turn",
        "kind": entry_info["kind"],
        "estimatedTravelMs": int(estimate_travel_ms(length, entry, measured_ms)),
        "brakeAtFraction": max(0.0, min(0.95, float(brake_at_fraction))),
        "brakeStrengthPct": float(brake_strength_pct),
    }


def _close_loop_path(path: list[dict[str, float]]) -> list[dict[str, float]]:
    """Redistribute end→start gap so the drawn parcours closes cleanly."""
    if len(path) < 2:
        return path
    sx, sy = path[0]["x"], path[0]["y"]
    ex, ey = path[-1]["x"], path[-1]["y"]
    dx, dy = sx - ex, sy - ey
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return path
    n = len(path) - 1
    closed: list[dict[str, float]] = []
    for i, p in enumerate(path):
        t = 0.0 if n <= 0 else i / n
        closed.append({"x": p["x"] + dx * t, "y": p["y"] + dy * t})
    return closed


def build_geometry(
    segments: list[dict[str, Any]],
    *,
    curve_gain: float = _CURVE_GAIN,
    close_loop: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """Build per-segment arcs + continuous race-circuit path (simple, no cross)."""
    gain = max(0.5, min(8.0, float(curve_gain)))
    raw_deltas = [heading_delta(float(s["left"]), float(s["right"]), gain) for s in segments]
    deltas = _scale_headings_to_loop(raw_deltas)

    x, y, heading = 0.0, 0.0, -math.pi / 2  # start facing up on screen
    path: list[dict[str, float]] = [{"x": x, "y": y}]
    ranges: list[tuple[int, int]] = []
    meta: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        left = float(seg["left"])
        right = float(seg["right"])
        length = max(segment_length(left, right), 1.0)
        d_head = deltas[i]
        start_idx = len(path) - 1
        steps = max(10, int(length / 2.5) + int(abs(d_head) * 18))
        for s in range(1, steps + 1):
            h = heading + d_head * (s / steps)
            step_len = length / steps
            x += math.cos(h) * step_len
            y += math.sin(h) * step_len
            path.append({"x": x, "y": y})
        heading += d_head
        end_idx = len(path) - 1
        ranges.append((start_idx, end_idx))
        meta.append(
            {
                "id": i,
                "left": left,
                "right": right,
                "length": round(length, 2),
                "turn": classify_turn(left, right),
                "headingDelta": round(d_head, 4),
                "curveGain": round(gain, 3),
                "maxSpeed": int(seg.get("maxSpeed", 700)),
            }
        )

    if close_loop:
        path = _close_loop_path(path)
    out: list[dict[str, Any]] = []
    for (start_idx, end_idx), base in zip(ranges, meta):
        seg_path = [
            {"x": round(path[j]["x"], 3), "y": round(path[j]["y"], 3)}
            for j in range(start_idx, end_idx + 1)
        ]
        out.append(
            {
                **base,
                "x0": seg_path[0]["x"],
                "y0": seg_path[0]["y"],
                "x1": seg_path[-1]["x"],
                "y1": seg_path[-1]["y"],
                "path": seg_path,
            }
        )
    if close_loop and path:
        closed = [{"x": round(p["x"], 3), "y": round(p["y"], 3)} for p in path]
        closed.append({"x": closed[0]["x"], "y": closed[0]["y"]})
        path = closed
    elif path:
        path = [{"x": round(p["x"], 3), "y": round(p["y"], 3)} for p in path]
    return out, path


class TrackController:
    def __init__(self, emit: Listener) -> None:
        self._emit = emit
        self.mapping = False
        self.mapping_vehicle: str | None = None
        self._skip_first = False
        self._lap1: list[dict[str, Any]] = []
        self._lap2: list[dict[str, Any]] = []
        self._raw: list[dict[str, Any]] = []  # alias = tour courant (compat preview/UI)
        self._laps_done = 0
        self._lap_len = 0
        self._verify_count = 0  # = nombre de bouts enregistrés au tour 2
        self._mapping_started_at = 0.0
        self._last_comparisons: dict[str, Any] | None = None
        self.latency_active = False
        self.latency_vehicle: str | None = None
        self.latency_started_at = 0.0
        self.latency_result: dict[str, Any] | None = None
        self.race_active = False
        self.race_vehicle: str | None = None
        self.start_segment = 0
        self.current_segment = 0
        self.part_count: int | None = None
        self.curve_gain: float = _CURVE_GAIN
        self.turn_slowdown_pct: float = 40.0  # % freinage virages (0=500, 100=0)
        self.straight_max_speed: int = _STRAIGHT_MAX_SPEED
        self.lookahead_segments: int = _DEFAULT_LOOKAHEAD  # 0=off, 1–3 bouts d’avance
        self.brake_at_fraction: float = _DEFAULT_BRAKE_AT  # 0=off, ~0.67=2/3, ~0.75=3/4
        self.brake_strength_pct: float = _DEFAULT_BRAKE_STRENGTH  # 0–150 %, 100 = vitesse du virage
        self._profile_cache: tuple[Any, list[dict[str, Any]]] | None = None
        self._seg_entered_at: float | None = None
        self._seg_travel_ms: dict[int, float] = {}
        self.map: dict[str, Any] | None = self._load_map()
        self.course_name: str | None = (self.map or {}).get("name") if self.map else None
        if self.map and self.map.get("partCount"):
            try:
                self.part_count = int(self.map["partCount"])
            except Exception:
                self.part_count = None
        if self.map and self.map.get("curveGain"):
            try:
                self.curve_gain = float(self.map["curveGain"])
            except Exception:
                self.curve_gain = _CURVE_GAIN
        self._restore_last_course()

    def _restore_last_course(self) -> None:
        """Au démarrage, reprendre la dernière map nommée si disponible."""
        try:
            pointer = _MAPS_DIR / "last_course.txt"
            if not pointer.exists():
                return
            name = pointer.read_text(encoding="utf-8").strip()
            if not name:
                return
            path = _COURSES_DIR / f"{_safe_course_name(name)}.json"
            if path.exists():
                self.load_course(name)
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        preview = self._preview_map() if self.mapping and self._lap1 else None
        last = None
        if self._lap2:
            last = self._lap2[-1]
        elif self._lap1:
            last = self._lap1[-1]
        return {
            "mapping": self.mapping,
            "mappingVehicle": self.mapping_vehicle,
            "rawCount": len(self._lap1),
            "lap1Count": len(self._lap1),
            "lap2Count": len(self._lap2),
            "lapsDone": self._laps_done,
            "lapsRequired": 2,
            "verifyCount": len(self._lap2),
            "lapLen": self._lap_len or len(self._lap1),
            "latencyActive": self.latency_active,
            "latencyResult": self.latency_result,
            "raceActive": self.race_active,
            "raceVehicle": self.race_vehicle,
            "startSegment": self.start_segment,
            "currentSegment": self.current_segment,
            "map": self.map,
            "preview": preview,
            "lastSample": last,
            "comparisons": self._last_comparisons,
            "courseName": self.course_name,
            "courses": self.list_courses(),
            "partCount": self.part_count or ((self.map or {}).get("partCount")),
            "curveGain": self.curve_gain,
            "turnSlowdownPct": self.turn_slowdown_pct,
            "straightMaxSpeed": self.straight_max_speed,
            "lookaheadSegments": self.lookahead_segments,
            "brakeAtFraction": self.brake_at_fraction,
            "brakeStrengthPct": self.brake_strength_pct,
            "raceProfile": self.race_profile(),
            "raceMatchRatio": _RACE_MATCH_RATIO,
        }

    def _preview_map(self) -> dict[str, Any] | None:
        # Dessin live : toujours basé sur le tour 1 (boucle complète dès qu’il est prêt)
        src = self._lap1 if self._lap1 else self._raw
        if not src:
            return None
        return self._map_from_raw(src, draft=True)

    def _map_from_raw(self, raw: list[dict[str, Any]], *, draft: bool = False) -> dict[str, Any]:
        part_count = int(self.part_count or len(raw) or 1)
        # Pendant le mapping (draft) : heuristique rapide. À la finalisation : optimisation.
        if draft:
            self.curve_gain = _seed_curve_gain(raw, part_count)
            opt_meta = {"crossed": False, "seed": round(self.curve_gain, 3), "draft": True}
        else:
            gain, opt_meta = optimize_curve_gain(raw, part_count)
            self.curve_gain = gain
        segments, path = build_geometry(raw, curve_gain=self.curve_gain, close_loop=True)
        # Si encore croisé, retente avec gains plus bas
        if _path_self_intersects(path):
            for g in (self.curve_gain * 0.75, self.curve_gain * 0.55, 1.0, 0.8):
                segments, path = build_geometry(raw, curve_gain=g, close_loop=True)
                if not _path_self_intersects(path):
                    self.curve_gain = g
                    opt_meta = {**opt_meta, "crossed": False, "fallbackGain": round(g, 3)}
                    break
        xs = [p["x"] for p in path] if path else [0.0]
        ys = [p["y"] for p in path] if path else [0.0]
        return {
            "createdAt": time.time(),
            "segmentCount": len(segments),
            "partCount": int(part_count),
            "curveGain": round(float(self.curve_gain), 3),
            "curveGainAuto": True,
            "loopClosed": True,
            "noCrossing": not _path_self_intersects(path),
            "optimize": opt_meta,
            "segments": segments,
            "path": path,
            "draft": draft,
            "bounds": {
                "minX": min(xs),
                "maxX": max(xs),
                "minY": min(ys),
                "maxY": max(ys),
            },
        }

    def rebuild_geometry(self) -> dict[str, Any]:
        """Recalcule le dessin (courbes amplifiées + boucle fermée) depuis les L/R."""
        if not self.map or not self.map.get("segments"):
            raise RuntimeError("Aucune map à redessiner")
        raw = [
            {
                "left": s["left"],
                "right": s["right"],
                "maxSpeed": s.get("maxSpeed", 700),
            }
            for s in self.map["segments"]
        ]
        name = self.map.get("name")
        start = self.map.get("startSegment", self.start_segment)
        saved_at = self.map.get("savedAt")
        rebuilt = self._map_from_raw(raw, draft=False)
        if name:
            rebuilt["name"] = name
        if saved_at:
            rebuilt["savedAt"] = saved_at
        rebuilt["startSegment"] = start
        self.map = rebuilt
        self._save_map()
        self._emit({"type": "mapUpdated", "map": self.map, "curveGain": self.curve_gain})
        return {"ok": True, "map": self.map}

    def _progress_event(self, vehicle_id: str, sample: dict[str, Any], **extra: Any) -> dict[str, Any]:
        preview = self._preview_map()
        bout_id = int(sample.get("id", self.current_segment))
        evt: dict[str, Any] = {
            "type": "mappingProgress",
            "vehicleId": vehicle_id,
            "count": len(self._lap1),
            "lap1Count": len(self._lap1),
            "lap2Count": len(self._lap2),
            "lapsDone": self._laps_done,
            "lapsRequired": 2,
            "verifyCount": len(self._lap2),
            "lapLen": self._lap_len or len(self._lap1),
            "sample": sample,
            "segmentId": bout_id,
            "preview": preview,
            "currentSegment": bout_id,
        }
        evt.update(extra)
        return evt

    def _load_map(self) -> dict[str, Any] | None:
        try:
            if _MAP_PATH.exists():
                return json.loads(_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def _save_map(self) -> None:
        if not self.map:
            return
        _MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        if self.course_name:
            self.map["name"] = self.course_name
        _MAP_PATH.write_text(json.dumps(self.map, indent=2), encoding="utf-8")

    def list_courses(self) -> list[dict[str, Any]]:
        _COURSES_DIR.mkdir(parents=True, exist_ok=True)
        out: list[dict[str, Any]] = []
        for path in sorted(_COURSES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(
                    {
                        "name": data.get("name") or path.stem,
                        "segmentCount": data.get("segmentCount") or len(data.get("segments") or []),
                        "createdAt": data.get("createdAt"),
                        "savedAt": data.get("savedAt"),
                        "file": path.name,
                    }
                )
            except Exception:
                continue
        return out

    def save_course(self, name: str) -> dict[str, Any]:
        if not self.map or not self.map.get("segments"):
            raise RuntimeError("Aucune map à sauvegarder — mappe d’abord la piste")
        safe = _safe_course_name(name)
        _COURSES_DIR.mkdir(parents=True, exist_ok=True)
        self.course_name = safe
        self.map["name"] = safe
        self.map["savedAt"] = time.time()
        self.map["startSegment"] = int(self.start_segment)
        path = _COURSES_DIR / f"{safe}.json"
        path.write_text(json.dumps(self.map, indent=2), encoding="utf-8")
        self._save_map()
        # Pointeur de reprise au prochain lancement
        (_MAPS_DIR / "last_course.txt").write_text(safe, encoding="utf-8")
        self._emit({"type": "courseSaved", "name": safe, "courses": self.list_courses(), "map": self.map})
        return {"ok": True, "name": safe, "courses": self.list_courses()}

    def load_course(self, name: str) -> dict[str, Any]:
        safe = _safe_course_name(name)
        path = _COURSES_DIR / f"{safe}.json"
        if not path.exists():
            raise RuntimeError(f"Map introuvable: {safe}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("segments"):
            raise RuntimeError("Fichier map invalide (pas de segments)")
        # Rebuild path drawing if older save lacks it
        if not data.get("path") and data.get("segments"):
            if data.get("curveGain") is not None:
                try:
                    self.curve_gain = float(data["curveGain"])
                except Exception:
                    pass
            rebuilt = self._map_from_raw(
                [
                    {
                        "left": s["left"],
                        "right": s["right"],
                        "maxSpeed": s.get("maxSpeed", 700),
                    }
                    for s in data["segments"]
                ],
                draft=False,
            )
            for i, seg in enumerate(rebuilt["segments"]):
                if i < len(data["segments"]):
                    seg["maxSpeed"] = int(data["segments"][i].get("maxSpeed", seg.get("maxSpeed", 700)))
            rebuilt["name"] = data.get("name") or safe
            rebuilt["savedAt"] = data.get("savedAt")
            rebuilt["startSegment"] = data.get("startSegment", 0)
            data = rebuilt
        self.map = data
        self.course_name = data.get("name") or safe
        n = max(1, len(data["segments"]))
        self.part_count = int(data.get("partCount") or n)
        self.map["partCount"] = self.part_count
        self.start_segment = int(data.get("startSegment", 0)) % n
        self.current_segment = self.start_segment
        self._save_map()
        (_MAPS_DIR / "last_course.txt").write_text(self.course_name, encoding="utf-8")
        self._emit(
            {
                "type": "courseLoaded",
                "name": self.course_name,
                "map": self.map,
                "startSegment": self.start_segment,
                "currentSegment": self.current_segment,
                "courses": self.list_courses(),
            }
        )
        return {"ok": True, "name": self.course_name, "map": self.map}

    def delete_course(self, name: str) -> dict[str, Any]:
        safe = _safe_course_name(name)
        path = _COURSES_DIR / f"{safe}.json"
        if path.exists():
            path.unlink()
        if self.course_name == safe:
            self.course_name = None
            if self.map:
                self.map.pop("name", None)
                self._save_map()
        self._emit({"type": "courseDeleted", "name": safe, "courses": self.list_courses()})
        return {"ok": True, "courses": self.list_courses()}

    def start_latency_test(self, vehicle_id: str) -> dict[str, Any]:
        self.latency_active = True
        self.latency_vehicle = vehicle_id
        self.latency_started_at = time.time()
        self.latency_result = None
        self._emit({"type": "latencyStarted", "vehicleId": vehicle_id})
        return {"ok": True, "vehicleId": vehicle_id}

    def start_mapping(self, vehicle_id: str, part_count: int | None = None) -> dict[str, Any]:
        # Couper course/latence + oublier l'ancienne map active (sinon elle bloque le live)
        if self.race_active:
            self.stop_race()
        if self.latency_active:
            self.latency_active = False
            self.latency_vehicle = None
        kept_parts = self.part_count
        self._clear_session_data(keep_courses=True)
        if part_count is not None:
            self.part_count = max(1, min(50, int(part_count)))
        elif kept_parts:
            self.part_count = kept_parts
        self.mapping = True
        self.mapping_vehicle = str(vehicle_id).strip()
        self._skip_first = True  # 1ʳᵉ mesure (souvent MQTT retenu) ignorée
        self._mapping_started_at = time.time()
        self._emit(
            {
                "type": "mappingStarted",
                "vehicleId": self.mapping_vehicle,
                "lapsRequired": 2,
                "partCount": self.part_count,
                "map": None,
                "preview": None,
                "rawCount": 0,
            }
        )
        return {
            "ok": True,
            "vehicleId": self.mapping_vehicle,
            "partCount": self.part_count,
            "mapping": True,
        }

    def stop_mapping(self, finalize: bool = False) -> dict[str, Any]:
        self.mapping = False
        vid = self.mapping_vehicle
        self.mapping_vehicle = None
        if finalize and len(self._lap1) >= _MIN_SEGMENTS:
            self._finalize_map()
            self._emit({"type": "mappingStopped", "vehicleId": vid, "map": self.map})
            return {"ok": True, "map": self.map}
        # Stop map = tout effacer, reprendre à 0
        return self.clear_mapping(vehicle_id=vid)

    def _clear_session_data(self, *, keep_courses: bool = True) -> None:
        """Efface map active + mesures. Les fichiers maps/courses/ restent si keep_courses."""
        self._lap1 = []
        self._lap2 = []
        self._raw = []
        self._laps_done = 0
        self._lap_len = 0
        self._verify_count = 0
        self._skip_first = False
        self._last_comparisons = None
        self.map = None
        self.course_name = None
        self.start_segment = 0
        self.current_segment = 0
        try:
            if _MAP_PATH.exists():
                _MAP_PATH.unlink()
        except Exception:
            pass
        # Ne pas toucher last_course.txt / courses/*.json (bibliothèque)

    def clear_mapping(self, vehicle_id: str | None = None) -> dict[str, Any]:
        """Annule le mapping en cours et remet les mesures à zéro."""
        self.mapping = False
        self.mapping_vehicle = None
        self._clear_session_data(keep_courses=True)
        self._emit(
            {
                "type": "mappingCleared",
                "vehicleId": vehicle_id,
                "map": None,
                "preview": None,
                "rawCount": 0,
                "courses": self.list_courses(),
            }
        )
        return {"ok": True, "cleared": True, "map": None, "courses": self.list_courses()}

    def set_start_segment(self, index: int) -> dict[str, Any]:
        if not self.map or not self.map.get("segments"):
            raise RuntimeError("Aucune map chargée")
        n = len(self.map["segments"])
        self.start_segment = int(index) % n
        self.current_segment = self.start_segment
        self._emit(
            {
                "type": "mapStartSegment",
                "startSegment": self.start_segment,
                "currentSegment": self.current_segment,
            }
        )
        return {"ok": True, "startSegment": self.start_segment}

    def set_segment_speed(self, index: int, max_speed: int) -> dict[str, Any]:
        if not self.map or not self.map.get("segments"):
            raise RuntimeError("Aucune map chargée")
        segs = self.map["segments"]
        i = int(index) % len(segs)
        segs[i]["maxSpeed"] = max(0, min(1000, int(max_speed)))
        self._save_map()
        self._emit({"type": "mapUpdated", "map": self.map})
        return {"ok": True, "map": self.map}

    def set_part_count(self, count: int) -> dict[str, Any]:
        n = max(1, min(50, int(count)))
        self.part_count = n
        if self.map and self.map.get("segments"):
            self.map["partCount"] = n
            # Recalcule le gain optimal pour ce nombre de bouts
            out = self.rebuild_geometry()
            out["partCount"] = n
            return out
        self._emit({"type": "partCountSet", "partCount": n})
        return {"ok": True, "partCount": n}

    def set_curve_gain(self, gain: float, *, rebuild: bool = True) -> dict[str, Any]:
        """Override manuel optionnel ; sinon le gain est calculé auto via rebuild_geometry."""
        self.curve_gain = max(0.5, min(8.0, float(gain)))
        if self.map:
            self.map["curveGain"] = self.curve_gain
            self.map["curveGainAuto"] = False
            if rebuild and self.map.get("segments"):
                raw = [
                    {"left": s["left"], "right": s["right"], "maxSpeed": s.get("maxSpeed", 700)}
                    for s in self.map["segments"]
                ]
                # Rebuild with forced manual gain (skip auto optimize once)
                segments, path = build_geometry(raw, curve_gain=self.curve_gain, close_loop=True)
                name = self.map.get("name")
                start = self.map.get("startSegment", self.start_segment)
                self.map = {
                    **self.map,
                    "segments": segments,
                    "path": path,
                    "segmentCount": len(segments),
                    "curveGain": round(self.curve_gain, 3),
                    "curveGainAuto": False,
                    "loopClosed": True,
                    "noCrossing": not _path_self_intersects(path),
                    "bounds": {
                        "minX": min(p["x"] for p in path),
                        "maxX": max(p["x"] for p in path),
                        "minY": min(p["y"] for p in path),
                        "maxY": max(p["y"] for p in path),
                    },
                }
                if name:
                    self.map["name"] = name
                self.map["startSegment"] = start
                self._save_map()
                self._emit({"type": "mapUpdated", "map": self.map, "curveGain": self.curve_gain})
                return {"ok": True, "map": self.map, "curveGain": self.curve_gain}
            self._save_map()
            self._emit({"type": "mapUpdated", "map": self.map, "curveGain": self.curve_gain})
        else:
            self._emit({"type": "curveGainSet", "curveGain": self.curve_gain})
        return {"ok": True, "curveGain": self.curve_gain}

    def set_turn_slowdown(self, pct: float) -> dict[str, Any]:
        self.turn_slowdown_pct = max(0.0, min(100.0, float(pct)))
        self._emit({"type": "turnSlowdownSet", "turnSlowdownPct": self.turn_slowdown_pct})
        return {"ok": True, "turnSlowdownPct": self.turn_slowdown_pct}

    def set_straight_max_speed(self, speed: int) -> dict[str, Any]:
        self.straight_max_speed = max(0, min(1000, int(speed)))
        self._emit(
            {
                "type": "straightMaxSet",
                "straightMaxSpeed": self.straight_max_speed,
            }
        )
        return {"ok": True, "straightMaxSpeed": self.straight_max_speed}

    def set_lookahead(
        self,
        segments: int,
        brake_at_fraction: float | None = None,
    ) -> dict[str, Any]:
        self.lookahead_segments = max(0, min(3, int(segments)))
        if brake_at_fraction is not None:
            self.brake_at_fraction = max(0.0, min(0.95, float(brake_at_fraction)))
        self._emit(
            {
                "type": "lookaheadSet",
                "lookaheadSegments": self.lookahead_segments,
                "brakeAtFraction": self.brake_at_fraction,
            }
        )
        return {
            "ok": True,
            "lookaheadSegments": self.lookahead_segments,
            "brakeAtFraction": self.brake_at_fraction,
        }

    def set_brake_strength(self, pct: float) -> dict[str, Any]:
        """Curseur de freinage avant virage (0–150 %, 100 = vitesse du virage)."""
        self.brake_strength_pct = max(0.0, min(150.0, float(pct)))
        self._emit(
            {
                "type": "brakeStrengthSet",
                "brakeStrengthPct": self.brake_strength_pct,
                "raceProfile": self.race_profile(),
            }
        )
        return {"ok": True, "brakeStrengthPct": self.brake_strength_pct}

    def race_profile(self) -> list[dict[str, Any]]:
        """Profil de vitesse précalculé (recalculé seulement si map/réglages changent)."""
        if not self.map or not self.map.get("segments"):
            return []
        segs = self.map["segments"]
        key = (
            id(self.map),
            len(segs),
            self.turn_slowdown_pct,
            self.straight_max_speed,
            self.lookahead_segments,
            self.brake_strength_pct,
        )
        cached = self._profile_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        profile = build_race_profile(
            segs,
            self.turn_slowdown_pct,
            straight_max=self.straight_max_speed,
            lookahead=self.lookahead_segments,
            brake_strength_pct=self.brake_strength_pct,
        )
        self._profile_cache = (key, profile)
        return profile

    def race_speed_plan(self, index: int | None = None) -> dict[str, Any]:
        if not self.map or not self.map.get("segments"):
            return {
                "segmentIndex": 0,
                "entrySpeed": 0,
                "brakeSpeed": 0,
                "brakeAfterMs": None,
                "length": 0.0,
                "isStraight": False,
            }
        segs = self.map["segments"]
        idx = self.current_segment if index is None else int(index)
        measured = self._seg_travel_ms.get(int(idx) % len(segs))
        return race_speed_plan(
            segs,
            idx,
            self.turn_slowdown_pct,
            straight_max=self.straight_max_speed,
            lookahead=self.lookahead_segments,
            brake_at_fraction=self.brake_at_fraction,
            measured_ms=measured,
            brake_strength_pct=self.brake_strength_pct,
            profile=self.race_profile(),
        )

    def target_speed_for_segment(self, index: int) -> int:
        """Vitesse d’entrée du bout (plein droit ; le frein mi-parcours est dans le plan)."""
        return int(self.race_speed_plan(index).get("entrySpeed") or 0)

    def start_race(self, vehicle_id: str) -> dict[str, Any]:
        if not self.map or not self.map.get("segments"):
            raise RuntimeError("Mappe d’abord la piste")
        self.race_active = True
        self.race_vehicle = vehicle_id
        self.current_segment = self.start_segment
        self._seg_entered_at = time.time()
        plan = self.race_speed_plan(self.current_segment)
        seg = self.map["segments"][self.current_segment]
        self._emit(
            {
                "type": "raceStarted",
                "vehicleId": vehicle_id,
                "currentSegment": self.current_segment,
                "targetSpeed": plan["entrySpeed"],
                "plan": plan,
                "turn": seg.get("turn"),
                "turnSlowdownPct": self.turn_slowdown_pct,
                "straightMaxSpeed": self.straight_max_speed,
                "lookaheadSegments": self.lookahead_segments,
                "brakeAtFraction": self.brake_at_fraction,
            }
        )
        return {
            "ok": True,
            "currentSegment": self.current_segment,
            "targetSpeed": plan["entrySpeed"],
            "plan": plan,
            "turnSlowdownPct": self.turn_slowdown_pct,
            "straightMaxSpeed": self.straight_max_speed,
            "lookaheadSegments": self.lookahead_segments,
            "brakeAtFraction": self.brake_at_fraction,
        }

    def stop_race(self) -> dict[str, Any]:
        self.race_active = False
        vid = self.race_vehicle
        self.race_vehicle = None
        self._seg_entered_at = None
        self._emit({"type": "raceStopped", "vehicleId": vid})
        return {"ok": True}

    def yield_to_manual(self, vehicle_id: str | None = None) -> dict[str, Any]:
        """Le cockpit (STOP / vitesse / voie) a toujours priorité sur course/mapping/latence."""
        vid_u = (vehicle_id or "").upper()
        stopped: list[str] = []

        if self.race_active and (not vid_u or (self.race_vehicle or "").upper() == vid_u):
            self.race_active = False
            self.race_vehicle = None
            self._seg_entered_at = None
            stopped.append("race")

        if self.mapping and (not vid_u or (self.mapping_vehicle or "").upper() == vid_u):
            self.mapping = False
            self.mapping_vehicle = None
            self._skip_first = False
            stopped.append("mapping")

        if self.latency_active and (not vid_u or (self.latency_vehicle or "").upper() == vid_u):
            self.latency_active = False
            self.latency_vehicle = None
            stopped.append("latency")

        if stopped:
            self._emit(
                {
                    "type": "manualOverride",
                    "vehicleId": vehicle_id,
                    "stopped": stopped,
                }
            )
        return {"ok": True, "stopped": stopped}

    def on_wheel_distance(self, vehicle_id: str, left: float, right: float, ts: Any = None) -> None:
        sample = {
            "left": float(left),
            "right": float(right),
            "turn": classify_turn(left, right),
            "length": segment_length(left, right),
            "ts": ts or time.time(),
        }
        self._emit(
            {
                "type": "wheelDistance",
                "vehicleId": vehicle_id,
                "left": sample["left"],
                "right": sample["right"],
                "turn": sample["turn"],
                "length": sample["length"],
            }
        )

        if self.latency_active and (self.latency_vehicle or "").upper() == str(vehicle_id).upper():
            elapsed = max(0.0, time.time() - self.latency_started_at)
            self.latency_active = False
            self.latency_result = {
                "vehicleId": vehicle_id,
                "elapsedMs": round(elapsed * 1000, 1),
                "left": sample["left"],
                "right": sample["right"],
                "avgDistance": sample["length"],
            }
            self._emit({"type": "latencyResult", "result": self.latency_result})
            return

        map_vid = (self.mapping_vehicle or "").upper()
        if self.mapping and map_vid and map_vid == str(vehicle_id).upper():
            # 1ʳᵉ mesure ignorée (souvent MQTT retenu) — une seule fois
            if self._skip_first:
                self._skip_first = False
                self._emit({"type": "mappingSkippedFirst", "vehicleId": vehicle_id})
                return

            sample["maxSpeed"] = 700
            n_parts = int(self.part_count) if self.part_count else 0

            # ——— Tour 2 : exactement n mesures, puis stop ———
            if self._laps_done >= 1 and self._lap_len > 0:
                bout_id = len(self._lap2)
                if bout_id >= self._lap_len:
                    return
                sample["id"] = bout_id
                expected = self._lap1[bout_id]
                matched = samples_match(sample, expected)
                conf = sample_confidence(sample, expected)
                self._lap2.append(dict(sample))
                self._raw = self._lap2
                self._verify_count = len(self._lap2)
                self.current_segment = bout_id
                self._emit(
                    self._progress_event(
                        vehicle_id,
                        sample,
                        matched=matched,
                        confidence=conf,
                        expected={"left": expected["left"], "right": expected["right"]},
                        kept=True,
                        targetTotal=self._lap_len * 2,
                        totalKept=len(self._lap1) + len(self._lap2),
                    )
                )
                if len(self._lap2) >= self._lap_len:
                    self._complete_mapping(vehicle_id)
                return

            # ——— Tour 1 ———
            # Si partCount connu : on prend exactement n bouts, puis on passe au tour 2.
            # Sans partCount : fermeture naturelle quand on retombe sur le 1er bout.
            if n_parts and len(self._lap1) >= n_parts:
                # Ne devrait plus arriver (passage tour 2 déjà fait) — sécurité
                self._laps_done = 1
                self._lap_len = len(self._lap1)
                # cette mesure = 1er bout du tour 2
                sample["id"] = 0
                expected = self._lap1[0]
                matched = samples_match(sample, expected)
                conf = sample_confidence(sample, expected)
                self._lap2 = [dict(sample)]
                self._raw = self._lap2
                self._verify_count = 1
                self.current_segment = 0
                self._emit(
                    self._progress_event(
                        vehicle_id,
                        sample,
                        matched=matched,
                        confidence=conf,
                        expected={"left": expected["left"], "right": expected["right"]},
                        lapClosed=True,
                        kept=True,
                        targetTotal=self._lap_len * 2,
                        totalKept=len(self._lap1) + len(self._lap2),
                    )
                )
                if len(self._lap2) >= self._lap_len:
                    self._complete_mapping(vehicle_id)
                return

            # Fermeture naturelle (seulement si pas de partCount) : retombe ≈ 1er bout
            if (
                not n_parts
                and len(self._lap1) >= _MIN_SEGMENTS
                and samples_match(sample, self._lap1[0])
            ):
                self._laps_done = 1
                self._lap_len = len(self._lap1)
                sample["id"] = 0
                conf = sample_confidence(sample, self._lap1[0])
                self._lap2 = [dict(sample)]
                self._raw = self._lap2
                self._verify_count = 1
                self.current_segment = 0
                self._emit(
                    self._progress_event(
                        vehicle_id,
                        sample,
                        matched=True,
                        confidence=conf,
                        expected={"left": self._lap1[0]["left"], "right": self._lap1[0]["right"]},
                        lapClosed=True,
                        kept=True,
                        targetTotal=self._lap_len * 2,
                        totalKept=len(self._lap1) + len(self._lap2),
                    )
                )
                if len(self._lap2) >= self._lap_len:
                    self._complete_mapping(vehicle_id)
                return

            # Continuer le tour 1
            sample["id"] = len(self._lap1)
            self._lap1.append(dict(sample))
            self._raw = self._lap1
            self.current_segment = sample["id"]
            self._emit(
                self._progress_event(
                    vehicle_id,
                    sample,
                    kept=True,
                    targetTotal=(n_parts * 2) if n_parts else None,
                    totalKept=len(self._lap1) + len(self._lap2),
                )
            )
            # Après exactement n bouts → tour 2 (les prochaines mesures)
            if n_parts and len(self._lap1) >= n_parts:
                self._laps_done = 1
                self._lap_len = len(self._lap1)
                self._emit(
                    {
                        "type": "mappingLapClosed",
                        "vehicleId": vehicle_id,
                        "lap": 1,
                        "lapLen": self._lap_len,
                        "targetTotal": self._lap_len * 2,
                    }
                )
            return

        race_vid = (self.race_vehicle or "").upper()
        if self.race_active and race_vid and race_vid == str(vehicle_id).upper() and self.map:
            segs = self.map["segments"]
            if not segs:
                return
            n = len(segs)
            # La mesure correspond au bout qu'on vient de parcourir (= current)
            finished_idx = self.current_segment % n
            expected = segs[finished_idx]
            matched = samples_match(sample, expected, ratio=_RACE_MATCH_RATIO)
            now = time.time()
            if self._seg_entered_at is not None:
                traveled = max(0.0, (now - self._seg_entered_at) * 1000.0)
                # Lissage léger si on a déjà une mesure pour ce bout
                prev = self._seg_travel_ms.get(finished_idx)
                if prev is not None and prev > 60:
                    traveled = 0.65 * prev + 0.35 * traveled
                self._seg_travel_ms[finished_idx] = traveled
            # Même si un peu bruité (jusqu'à ~1.3), on avance au bout suivant
            next_idx = (finished_idx + 1) % n
            self.current_segment = next_idx
            self._seg_entered_at = now
            plan = self.race_speed_plan(next_idx)
            nxt = segs[next_idx]
            self._emit(
                {
                    "type": "mapPosition",
                    "vehicleId": vehicle_id,
                    "currentSegment": self.current_segment,
                    "finishedSegment": finished_idx,
                    "startSegment": self.start_segment,
                    "partCount": self.map.get("partCount") or n,
                    "targetSpeed": plan["entrySpeed"],
                    "plan": plan,
                    "turn": nxt.get("turn"),
                    "tightness": round(turn_tightness(nxt.get("left", 0), nxt.get("right", 0)), 3),
                    "turnSlowdownPct": self.turn_slowdown_pct,
                    "straightMaxSpeed": self.straight_max_speed,
                    "lookaheadSegments": self.lookahead_segments,
                    "brakeAtFraction": self.brake_at_fraction,
                    "matched": matched,
                    "matchRatio": _RACE_MATCH_RATIO,
                    "sample": {"left": sample["left"], "right": sample["right"]},
                    "expected": {"left": expected.get("left"), "right": expected.get("right")},
                }
            )

    def _complete_mapping(self, vehicle_id: str) -> None:
        """Fin des 2 tours (n×2 mesures) : finalise la map et émet mappingComplete."""
        self._laps_done = 2
        self.mapping = False
        completed_vid = self.mapping_vehicle or vehicle_id
        self.mapping_vehicle = None
        self._finalize_map()
        self._emit(
            {
                "type": "mappingComplete",
                "vehicleId": completed_vid,
                "map": self.map,
                "lapsDone": 2,
                "lapLen": self._lap_len,
                "totalKept": len(self._lap1) + len(self._lap2),
                "targetTotal": (self._lap_len * 2) if self._lap_len else None,
                "comparisons": self._last_comparisons,
            }
        )

    def _finalize_map(self) -> None:
        """Fusionne tour1+tour2 (moyenne), numérote les bouts, calcule la confiance."""
        if not self._lap1:
            return
        if not self.part_count:
            self.part_count = len(self._lap1)
        merged, comparisons = merge_lap_samples(self._lap1, self._lap2)
        self._last_comparisons = comparisons
        # Géométrie depuis les mesures fusionnées
        self.map = self._map_from_raw(merged, draft=False)
        # Réinjecter métadonnées de comparaison / numéros sur les segments géométrie
        for i, seg in enumerate(self.map.get("segments") or []):
            if i < len(merged):
                src = merged[i]
                seg["id"] = src["id"]
                seg["left"] = src["left"]
                seg["right"] = src["right"]
                seg["turn"] = src["turn"]
                seg["length"] = src["length"]
                seg["lap1"] = src.get("lap1")
                seg["lap2"] = src.get("lap2")
                seg["matched"] = src.get("matched")
                seg["confidence"] = src.get("confidence")
        self.map["partCount"] = int(self.part_count)
        self.map["loopClosed"] = True
        self.map["comparisons"] = comparisons
        self.map["lap1"] = [
            {"id": s.get("id", i), "left": s["left"], "right": s["right"], "turn": s.get("turn")}
            for i, s in enumerate(self._lap1)
        ]
        self.map["lap2"] = [
            {"id": s.get("id", i), "left": s["left"], "right": s["right"], "turn": s.get("turn")}
            for i, s in enumerate(self._lap2)
        ]
        self._raw = merged
        self.start_segment = 0
        self.current_segment = 0
        self._save_map()

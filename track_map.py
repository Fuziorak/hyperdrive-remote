"""
Track mapping from Anki wheelDistance traits.

wheelDistance value: { left, right } — distance since previous trait.
right < left → turn toward the right (right wheel travelled less).
First sample after starting mapping is discarded (not trait→trait).
Needs 2 full laps: lap 1 builds the segment list; lap 2 must repeat
each measure identically (within tolerance) before the map is finalized.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[dict[str, Any]], None]

# Approximate axle width for heading delta from wheel travel (cm-like units from lab).
_AXLE = 48.0
# Relative match: values may differ by ~10% (ratio ≤ 1.1), not 1:1 exact.
_MATCH_RATIO = 1.1
# Floor so tiny distances still match without exploding the ratio.
_MATCH_ABS_FLOOR = 3.0
_MIN_SEGMENTS = 3
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


def heading_delta(left: float, right: float) -> float:
    """Positive = turn right (clockwise if +Y forward)."""
    return (float(left) - float(right)) / _AXLE


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


def build_geometry(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """Build per-segment meta + continuous path points for SVG drawing."""
    x, y, heading = 0.0, 0.0, -math.pi / 2  # start facing up on screen
    path: list[dict[str, float]] = [{"x": x, "y": y}]
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        left = float(seg["left"])
        right = float(seg["right"])
        length = max(segment_length(left, right), 1.0)
        d_head = heading_delta(left, right)
        x0, y0 = x, y
        seg_path = [{"x": x0, "y": y0}]
        # March along an arc approximated by heading mid-step
        steps = max(6, int(length / 4) or 6)
        for s in range(1, steps + 1):
            t = s / steps
            h = heading + d_head * t
            step_len = length / steps
            x += math.cos(h) * step_len
            y += math.sin(h) * step_len
            pt = {"x": round(x, 3), "y": round(y, 3)}
            seg_path.append(pt)
            path.append(pt)
        heading += d_head
        out.append(
            {
                "id": i,
                "left": left,
                "right": right,
                "length": round(length, 2),
                "turn": classify_turn(left, right),
                "headingDelta": round(d_head, 4),
                "maxSpeed": int(seg.get("maxSpeed", 700)),
                "x0": round(x0, 3),
                "y0": round(y0, 3),
                "x1": round(x, 3),
                "y1": round(y, 3),
                "path": seg_path,
            }
        )
    # Close the loop visually toward the start point
    if path:
        path.append({"x": path[0]["x"], "y": path[0]["y"]})
    return out, path


class TrackController:
    def __init__(self, emit: Listener) -> None:
        self._emit = emit
        self.mapping = False
        self.mapping_vehicle: str | None = None
        self._skip_first = False
        self._raw: list[dict[str, Any]] = []
        self._laps_done = 0
        self._lap_len = 0
        self._verify_count = 0
        self.latency_active = False
        self.latency_vehicle: str | None = None
        self.latency_started_at = 0.0
        self.latency_result: dict[str, Any] | None = None
        self.race_active = False
        self.race_vehicle: str | None = None
        self.start_segment = 0
        self.current_segment = 0
        self.map: dict[str, Any] | None = self._load_map()
        self.course_name: str | None = (self.map or {}).get("name") if self.map else None
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
        preview = self._preview_map() if self.mapping and self._raw else None
        return {
            "mapping": self.mapping,
            "mappingVehicle": self.mapping_vehicle,
            "rawCount": len(self._raw),
            "lapsDone": self._laps_done,
            "lapsRequired": 2,
            "verifyCount": self._verify_count,
            "lapLen": self._lap_len,
            "latencyActive": self.latency_active,
            "latencyResult": self.latency_result,
            "raceActive": self.race_active,
            "raceVehicle": self.race_vehicle,
            "startSegment": self.start_segment,
            "currentSegment": self.current_segment,
            "map": self.map,
            "preview": preview,
            "lastSample": self._raw[-1] if self._raw else None,
            "courseName": self.course_name,
            "courses": self.list_courses(),
        }

    def _preview_map(self) -> dict[str, Any] | None:
        if not self._raw:
            return None
        return self._map_from_raw(self._raw, draft=True)

    def _map_from_raw(self, raw: list[dict[str, Any]], *, draft: bool = False) -> dict[str, Any]:
        segments, path = build_geometry(raw)
        xs = [p["x"] for p in path] if path else [0.0]
        ys = [p["y"] for p in path] if path else [0.0]
        return {
            "createdAt": time.time(),
            "segmentCount": len(segments),
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

    def _progress_event(self, vehicle_id: str, sample: dict[str, Any], **extra: Any) -> dict[str, Any]:
        preview = self._preview_map()
        evt: dict[str, Any] = {
            "type": "mappingProgress",
            "vehicleId": vehicle_id,
            "count": len(self._raw),
            "lapsDone": self._laps_done,
            "lapsRequired": 2,
            "verifyCount": self._verify_count,
            "lapLen": self._lap_len,
            "sample": sample,
            "preview": preview,
            "currentSegment": max(0, len(self._raw) - 1),
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

    def start_mapping(self, vehicle_id: str) -> dict[str, Any]:
        self.mapping = True
        self.mapping_vehicle = vehicle_id
        self._skip_first = True
        self._raw = []
        self._laps_done = 0
        self._lap_len = 0
        self._verify_count = 0
        self._emit({"type": "mappingStarted", "vehicleId": vehicle_id, "lapsRequired": 2})
        return {"ok": True, "vehicleId": vehicle_id}

    def stop_mapping(self, finalize: bool = False) -> dict[str, Any]:
        self.mapping = False
        vid = self.mapping_vehicle
        self.mapping_vehicle = None
        if finalize and len(self._raw) >= _MIN_SEGMENTS:
            self._finalize_map()
        self._emit({"type": "mappingStopped", "vehicleId": vid, "map": self.map})
        return {"ok": True, "map": self.map}

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

    def start_race(self, vehicle_id: str) -> dict[str, Any]:
        if not self.map or not self.map.get("segments"):
            raise RuntimeError("Mappe d’abord la piste")
        self.race_active = True
        self.race_vehicle = vehicle_id
        self.current_segment = self.start_segment
        speed = int(self.map["segments"][self.current_segment].get("maxSpeed", 700))
        self._emit(
            {
                "type": "raceStarted",
                "vehicleId": vehicle_id,
                "currentSegment": self.current_segment,
                "targetSpeed": speed,
            }
        )
        return {"ok": True, "currentSegment": self.current_segment, "targetSpeed": speed}

    def stop_race(self) -> dict[str, Any]:
        self.race_active = False
        vid = self.race_vehicle
        self.race_vehicle = None
        self._emit({"type": "raceStopped", "vehicleId": vid})
        return {"ok": True}

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
            if self._skip_first:
                self._skip_first = False
                self._emit({"type": "mappingSkippedFirst", "vehicleId": vehicle_id})
                return

            # Tour 2 : chaque mesure doit refrapper celle du tour 1
            if self._laps_done >= 1 and self._lap_len > 0:
                expected = self._raw[self._verify_count % self._lap_len]
                matched = samples_match(sample, expected)
                if matched:
                    self._verify_count += 1
                self.current_segment = min(self._verify_count, self._lap_len - 1)
                self._emit(
                    self._progress_event(
                        vehicle_id,
                        sample,
                        matched=matched,
                        expected={"left": expected["left"], "right": expected["right"]},
                    )
                )
                if self._verify_count >= self._lap_len:
                    self._laps_done = 2
                    self.mapping = False
                    self.mapping_vehicle = None
                    self._finalize_map()
                    self._emit({"type": "mappingComplete", "map": self.map, "lapsDone": 2})
                return

            # Tour 1 : fermeture quand on retrouve la 1ʳᵉ mesure gardée
            if len(self._raw) >= _MIN_SEGMENTS and samples_match(sample, self._raw[0]):
                self._laps_done = 1
                self._lap_len = len(self._raw)
                self._verify_count = 1  # cette mesure = 1ʳᵉ du tour 2
                self.current_segment = 0
                self._emit(self._progress_event(vehicle_id, sample, matched=True))
                return

            sample["maxSpeed"] = 700
            self._raw.append(sample)
            self.current_segment = len(self._raw) - 1
            self._emit(self._progress_event(vehicle_id, sample))
            return

        race_vid = (self.race_vehicle or "").upper()
        if self.race_active and race_vid and race_vid == str(vehicle_id).upper() and self.map:
            segs = self.map["segments"]
            if not segs:
                return
            self.current_segment = (self.current_segment + 1) % len(segs)
            speed = int(segs[self.current_segment].get("maxSpeed", 700))
            self._emit(
                {
                    "type": "mapPosition",
                    "vehicleId": vehicle_id,
                    "currentSegment": self.current_segment,
                    "startSegment": self.start_segment,
                    "targetSpeed": speed,
                    "turn": segs[self.current_segment].get("turn"),
                }
            )

    def _finalize_map(self) -> None:
        self.map = self._map_from_raw(self._raw, draft=False)
        self.start_segment = 0
        self.current_segment = 0
        self._save_map()

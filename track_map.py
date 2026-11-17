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
import time
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[dict[str, Any]], None]

# Approximate axle width for heading delta from wheel travel (cm-like units from lab).
_AXLE = 48.0
_MATCH_TOL = 4.0
_MIN_SEGMENTS = 3
_MAP_PATH = Path(__file__).resolve().parent / "maps" / "track.json"


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


def samples_match(a: dict[str, Any], b: dict[str, Any], tol: float = _MATCH_TOL) -> bool:
    return abs(float(a["left"]) - float(b["left"])) <= tol and abs(float(a["right"]) - float(b["right"])) <= tol


def build_geometry(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build polyline points + per-segment meta for SVG drawing."""
    x, y, heading = 0.0, 0.0, -math.pi / 2  # start facing up on screen
    points = [{"x": x, "y": y}]
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        left = float(seg["left"])
        right = float(seg["right"])
        length = segment_length(left, right)
        d_head = heading_delta(left, right)
        # March along an arc approximated by heading mid-step
        steps = max(4, int(length / 8) or 4)
        for s in range(1, steps + 1):
            t = s / steps
            h = heading + d_head * t
            # chord step
            step_len = length / steps
            x += math.cos(h) * step_len
            y += math.sin(h) * step_len
            if s == steps:
                points.append({"x": x, "y": y})
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
                "x0": points[-2]["x"],
                "y0": points[-2]["y"],
                "x1": points[-1]["x"],
                "y1": points[-1]["y"],
            }
        )
    return out


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

    def snapshot(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping,
            "mappingVehicle": self.mapping_vehicle,
            "rawCount": len(self._raw),
            "lapsDone": self._laps_done,
            "lapsRequired": 2,
            "verifyCount": self._verify_count,
            "latencyActive": self.latency_active,
            "latencyResult": self.latency_result,
            "raceActive": self.race_active,
            "raceVehicle": self.race_vehicle,
            "startSegment": self.start_segment,
            "currentSegment": self.current_segment,
            "map": self.map,
        }

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
        _MAP_PATH.write_text(json.dumps(self.map, indent=2), encoding="utf-8")

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

        if self.latency_active and self.latency_vehicle == vehicle_id:
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

        if self.mapping and self.mapping_vehicle == vehicle_id:
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
                self._emit(
                    {
                        "type": "mappingProgress",
                        "vehicleId": vehicle_id,
                        "count": len(self._raw),
                        "lapsDone": self._laps_done,
                        "lapsRequired": 2,
                        "verifyCount": self._verify_count,
                        "lapLen": self._lap_len,
                        "matched": matched,
                        "sample": sample,
                        "expected": {"left": expected["left"], "right": expected["right"]},
                    }
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
                self._emit(
                    {
                        "type": "mappingProgress",
                        "vehicleId": vehicle_id,
                        "count": len(self._raw),
                        "lapsDone": 1,
                        "lapsRequired": 2,
                        "verifyCount": self._verify_count,
                        "lapLen": self._lap_len,
                        "matched": True,
                        "sample": sample,
                    }
                )
                return

            sample["maxSpeed"] = 700
            self._raw.append(sample)
            self._emit(
                {
                    "type": "mappingProgress",
                    "vehicleId": vehicle_id,
                    "count": len(self._raw),
                    "lapsDone": 0,
                    "lapsRequired": 2,
                    "sample": sample,
                }
            )
            return

        if self.race_active and self.race_vehicle == vehicle_id and self.map:
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
        segments = build_geometry(self._raw)
        xs = [s["x0"] for s in segments] + [s["x1"] for s in segments]
        ys = [s["y0"] for s in segments] + [s["y1"] for s in segments]
        self.map = {
            "createdAt": time.time(),
            "segmentCount": len(segments),
            "segments": segments,
            "bounds": {
                "minX": min(xs) if xs else 0,
                "maxX": max(xs) if xs else 1,
                "minY": min(ys) if ys else 0,
                "maxY": max(ys) if ys else 1,
            },
        }
        self.start_segment = 0
        self.current_segment = 0
        self._save_map()

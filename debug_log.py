"""
Persistent debug logger for Hyperdrive Remote.

Writes to debug.log next to the app so you can reproduce on Wi-Fi hyperdrive,
then read the file later on another network.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PATH = Path(__file__).resolve().parent / "debug.log"
_lock = threading.Lock()
_session_id = datetime.now().strftime("%Y%m%d-%H%M%S")


def log_path() -> Path:
    return LOG_PATH


def clear() -> None:
    with _lock:
        LOG_PATH.write_text("", encoding="utf-8")


def write(level: str, source: str, message: str, **data: Any) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
    line_obj: dict[str, Any] = {
        "ts": ts,
        "session": _session_id,
        "level": level.upper(),
        "source": source,
        "message": message,
    }
    if data:
        # Keep payloads readable / bounded
        safe: dict[str, Any] = {}
        for key, value in data.items():
            try:
                text = json.dumps(value, default=str)
            except Exception:
                text = repr(value)
            if len(text) > 2000:
                text = text[:2000] + "…(truncated)"
                safe[key] = text
            else:
                safe[key] = value
        line_obj["data"] = safe

    line = json.dumps(line_obj, ensure_ascii=False, default=str)
    with _lock:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def info(source: str, message: str, **data: Any) -> None:
    write("INFO", source, message, **data)


def warn(source: str, message: str, **data: Any) -> None:
    write("WARN", source, message, **data)


def error(source: str, message: str, **data: Any) -> None:
    write("ERROR", source, message, **data)


def exception(source: str, message: str, exc: BaseException | None = None, **data: Any) -> None:
    payload = dict(data)
    if exc is not None:
        payload["exc_type"] = type(exc).__name__
        payload["exc"] = str(exc)
        payload["traceback"] = traceback.format_exc()
    write("ERROR", source, message, **payload)


def probe_broker(host: str = "10.42.0.1", ports: tuple[int, ...] = (1883, 9001, 18443)) -> dict[str, Any]:
    results: dict[str, Any] = {"host": host, "ports": {}}
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                results["ports"][str(port)] = "open"
        except OSError as exc:
            results["ports"][str(port)] = f"fail:{type(exc).__name__}:{exc}"
    return results


def collect_network_snapshot() -> dict[str, Any]:
    snap: dict[str, Any] = {"brokerProbe": probe_broker()}
    try:
        out = subprocess.check_output(
            ["ipconfig"],
            text=True,
            errors="replace",
            timeout=8,
        )
        # Keep only useful lines to avoid huge logs
        keep = []
        for line in out.splitlines():
            low = line.lower()
            if any(
                k in low
                for k in (
                    "wi-fi",
                    "wifi",
                    "wireless",
                    "ethernet",
                    "ipv4",
                    "passerelle",
                    "gateway",
                    "10.42",
                    "10.11",
                    "adapter",
                    "carte",
                )
            ):
                keep.append(line.rstrip())
        snap["ipconfig_filtered"] = keep[-80:]
    except Exception as exc:
        snap["ipconfig_error"] = f"{type(exc).__name__}: {exc}"

    try:
        out = subprocess.check_output(
            ["ping", "-n", "2", "-w", "1000", "10.42.0.1"],
            text=True,
            errors="replace",
            timeout=8,
        )
        snap["ping_10_42_0_1"] = out.strip().splitlines()[-8:]
    except Exception as exc:
        snap["ping_error"] = f"{type(exc).__name__}: {exc}"

    return snap


def session_banner(extra: dict[str, Any] | None = None) -> None:
    info(
        "system",
        "=== new debug session ===",
        logFile=str(LOG_PATH),
        **(extra or {}),
    )
    try:
        net = collect_network_snapshot()
        info("system", "network snapshot at session start", **net)
    except Exception as exc:
        exception("system", "network snapshot failed", exc)

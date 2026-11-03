"""Helpers for paho-mqtt 2.x ReasonCode objects."""

from __future__ import annotations

from typing import Any


def mqtt_rc_ok(reason_code: Any) -> bool:
    """True when CONNACK / disconnect reason means success."""
    if reason_code is None:
        return False
    if hasattr(reason_code, "is_failure"):
        try:
            return not bool(reason_code.is_failure)
        except Exception:
            pass
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        name = str(reason_code).lower()
        return name in {"0", "success", "normal disconnection"}


def mqtt_rc_str(reason_code: Any) -> str:
    return str(reason_code)

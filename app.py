"""
Hyperdrive Remote — local web UI + MQTT orchestrator bridge.

Open http://127.0.0.1:5050 after joining Wi-Fi SSID `hyperdrive`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock

import debug_log
from mqtt_bridge import MqttBridge
from orchestrator import LIGHT_CHANNELS, LIGHT_EFFECTS

app = Flask(__name__, static_folder="static", static_url_path="/static")
sock = Sock(app)

# Couche MQTT reconstruite (minimal, alignée MQTT Explorer).
orch = MqttBridge()
_ws_clients: list[Any] = []
_ws_lock = threading.Lock()
_had_ui_client = False
_idle_shutdown_token = 0
# Grace so a page refresh does not kill the server mid-reload.
_IDLE_SHUTDOWN_S = 3.0


def _shutdown_process(reason: str) -> None:
    debug_log.info("system", "shutting down server", reason=reason)
    try:
        orch.disconnect_broker()
    except Exception:
        pass
    # Hard exit: Flask's threaded server has no clean stop API.
    os._exit(0)


def _schedule_idle_shutdown() -> None:
    """Quit when the browser tab/window is gone (last WebSocket closed)."""
    global _idle_shutdown_token
    with _ws_lock:
        _idle_shutdown_token += 1
        token = _idle_shutdown_token

    def _watch() -> None:
        time.sleep(_IDLE_SHUTDOWN_S)
        with _ws_lock:
            if token != _idle_shutdown_token:
                return
            if _ws_clients:
                return
            if not _had_ui_client:
                return
        _shutdown_process("no UI clients left")

    threading.Thread(target=_watch, name="idle-shutdown", daemon=True).start()


def _cancel_idle_shutdown() -> None:
    global _idle_shutdown_token
    with _ws_lock:
        _idle_shutdown_token += 1


def _broadcast(event: dict[str, Any]) -> None:
    # Avoid flooding the file with high-frequency telemetry; keep key events.
    et = event.get("type")
    if et in {
        "mqttConnected",
        "mqttDisconnected",
        "mqttError",
        "vehicleDiscovered",
        "scanning",
        "assertControl",
        "holdChanged",
        "subscription",
    }:
        debug_log.info("event", f"broadcast {et}", event=event)
    elif et == "mqttOut":
        debug_log.info("event", "mqtt out", topic=event.get("topic"), payload=event.get("payload"))
    raw = json.dumps(event, default=str)
    dead: list[Any] = []
    with _ws_lock:
        clients = list(_ws_clients)
    for ws in clients:
        try:
            ws.send(raw)
        except Exception:
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                if ws in _ws_clients:
                    _ws_clients.remove(ws)


orch.add_listener(_broadcast)


@app.before_request
def _log_request() -> None:
    if request.path.startswith("/static"):
        return
    if request.path == "/api/debug/client":
        return
    body = None
    if request.method in {"POST", "PUT", "PATCH"}:
        body = request.get_json(silent=True)
    debug_log.info(
        "http",
        f"{request.method} {request.path}",
        body=body,
        args=dict(request.args),
    )


@app.after_request
def _log_response(resp):  # type: ignore[no-untyped-def]
    if request.path.startswith("/static") or request.path == "/api/debug/client":
        return resp
    if request.path.startswith("/api") or request.path == "/":
        debug_log.info(
            "http",
            f"response {request.method} {request.path}",
            status=resp.status_code,
        )
    return resp


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def api_state():
    return jsonify(orch.snapshot())


@app.get("/api/meta")
def api_meta():
    return jsonify(
        {
            "lightChannels": list(LIGHT_CHANNELS),
            "lightEffects": list(LIGHT_EFFECTS),
            "presets": ["off", "green", "red", "blue", "police", "tailFlash", "strobe"],
            "defaults": {
                "brokerHost": orch.broker_host,
                "brokerPort": orch.broker_port,
                "hostId": orch.host_id,
                "mqttUsername": orch.mqtt_username,
            },
        }
    )


@app.post("/api/broker/connect")
def api_broker_connect():
    data = request.get_json(silent=True) or {}
    orch.configure(
        broker_host=data.get("brokerHost"),
        broker_port=int(data["brokerPort"]) if data.get("brokerPort") else None,
        host_id=data.get("hostId"),
        mqtt_username=data.get("mqttUsername"),
        mqtt_password=data.get("mqttPassword"),
    )
    # Default force=False so autoBoot does not murder an in-progress handshake.
    force = bool(data.get("force", False))
    try:
        # Snapshot in background — must not delay the MQTT handshake (was ~3s).
        def _snap() -> None:
            try:
                net = debug_log.collect_network_snapshot()
                debug_log.info("api", "broker connect: network snapshot", **net)
            except Exception as exc:
                debug_log.exception("api", "network snapshot failed", exc)

        threading.Thread(target=_snap, name="net-snap", daemon=True).start()
        reachable = orch.broker_reachable()
        orch.connect_broker(force=force)
        snap = orch.snapshot()
        snap["brokerReachable"] = reachable
        debug_log.info("api", "broker connect requested", reachable=reachable, force=force)
        return jsonify({"ok": True, "state": snap, "brokerReachable": reachable})
    except Exception as exc:
        debug_log.exception("api", "broker connect failed", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/broker/ping")
def api_broker_ping():
    reachable = orch.broker_reachable()
    return jsonify(
        {
            "ok": True,
            "reachable": reachable,
            "broker": f"{orch.broker_host}:{orch.broker_port}",
            "mqttConnected": orch.is_connected,
            "state": orch.snapshot(),
        }
    )


@app.post("/api/broker/disconnect")
def api_broker_disconnect():
    orch.disconnect_broker()
    return jsonify({"ok": True, "state": orch.snapshot()})


@app.post("/api/discover")
def api_discover():
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enable", True))
    try:
        orch.discover(enable)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/manual")
def api_manual_vehicle():
    data = request.get_json(silent=True) or {}
    vehicle_id = (data.get("vehicleId") or "").strip()
    model = data.get("model")
    try:
        orch.register_vehicle_manual(vehicle_id, model=model)
        return jsonify({"ok": True, "state": orch.snapshot()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/connect")
def api_connect(vehicle_id: str):
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enable", True))
    try:
        orch.connect_vehicle(vehicle_id, enable=enable)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/speed")
def api_speed(vehicle_id: str):
    data = request.get_json(silent=True) or {}
    try:
        orch.set_speed(
            vehicle_id,
            velocity=int(data.get("velocity", 0)),
            acceleration=int(data.get("acceleration", 1000)),
        )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/lane")
def api_lane(vehicle_id: str):
    data = request.get_json(silent=True) or {}
    try:
        orch.set_lane(
            vehicle_id,
            offset=float(data.get("offset", 0)),
            velocity=int(data.get("velocity", 300)),
            acceleration=int(data.get("acceleration", 800)),
            offset_from_center=data.get("offsetFromCenter"),
        )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/cancel-lane")
def api_cancel_lane(vehicle_id: str):
    try:
        orch.cancel_lane(vehicle_id)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/lights")
def api_lights(vehicle_id: str):
    data = request.get_json(silent=True) or {}
    try:
        if data.get("preset"):
            orch.apply_light_preset(vehicle_id, data["preset"])
        else:
            orch.set_lights(vehicle_id, data.get("lights") or data)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/panic")
def api_panic(vehicle_id: str):
    try:
        orch.panic_stop(vehicle_id)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/assert")
def api_assert(vehicle_id: str):
    """Re-send remembered settings to override other clients on the shared network."""
    try:
        applied = orch.assert_control(vehicle_id)
        return jsonify({"ok": True, "applied": applied})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/vehicles/<vehicle_id>/hold")
def api_hold(vehicle_id: str):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enable", False))
    interval = float(data.get("interval", 1.5))
    try:
        orch.set_hold(vehicle_id, enabled=enabled, interval_s=max(0.5, min(10.0, interval)))
        return jsonify({"ok": True, "state": orch.snapshot()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/debug/info")
def api_debug_info():
    return jsonify(
        {
            "ok": True,
            "logFile": str(debug_log.log_path()),
            "exists": debug_log.log_path().exists(),
            "size": debug_log.log_path().stat().st_size if debug_log.log_path().exists() else 0,
        }
    )


@app.get("/api/debug/tail")
def api_debug_tail():
    n = max(20, min(500, int(request.args.get("n", 120))))
    path = debug_log.log_path()
    if not path.exists():
        return jsonify({"ok": True, "lines": [], "logFile": str(path)})
    lines = path.read_text(encoding="utf-8").splitlines()
    return jsonify({"ok": True, "lines": lines[-n:], "logFile": str(path)})


@app.post("/api/debug/client")
def api_debug_client():
    data = request.get_json(silent=True) or {}
    level = str(data.get("level") or "INFO").upper()
    message = str(data.get("message") or "ui event")
    payload = data.get("data") if isinstance(data.get("data"), dict) else {"data": data.get("data")}
    debug_log.write(level, "ui", message, **(payload or {}))
    return jsonify({"ok": True})


@app.post("/api/debug/clear")
def api_debug_clear():
    debug_log.clear()
    debug_log.session_banner({"callerId": orch.caller_id, "note": "log cleared by user"})
    return jsonify({"ok": True, "logFile": str(debug_log.log_path())})


@app.post("/api/debug/snapshot")
def api_debug_snapshot():
    net = debug_log.collect_network_snapshot()
    debug_log.info("api", "manual network snapshot", **net)
    return jsonify({"ok": True, "snapshot": net, "logFile": str(debug_log.log_path())})


@sock.route("/ws")
def ws_events(ws):
    global _had_ui_client
    with _ws_lock:
        _ws_clients.append(ws)
        _had_ui_client = True
    _cancel_idle_shutdown()
    debug_log.info("ws", "client connected", clients=len(_ws_clients))
    try:
        ws.send(json.dumps({"type": "hello", "state": orch.snapshot()}))
        while True:
            # Keep the socket open; UI commands go through REST.
            message = ws.receive()
            if message is None:
                break
    finally:
        with _ws_lock:
            if ws in _ws_clients:
                _ws_clients.remove(ws)
            remaining = len(_ws_clients)
        debug_log.info("ws", "client disconnected", clients=remaining)
        if remaining == 0:
            _schedule_idle_shutdown()


def _warmup_lab_connection() -> None:
    """Best-effort MQTT connect + discover as soon as the server starts."""
    import time

    def _run() -> None:
        time.sleep(0.4)
        try:
            debug_log.info("warmup", "starting MQTT warmup")
            orch.connect_broker(force=True)
            # Give the async MQTT handshake a moment; UI also auto-scans.
            for _ in range(20):
                time.sleep(0.25)
                if orch.is_connected:
                    orch.discover(True)
                    msg = f"Auto MQTT OK ({orch.broker_host}) · scan started · caller={orch.caller_id}"
                    print(msg)
                    debug_log.info("warmup", msg)
                    return
            print("Auto MQTT: broker not reachable yet (join Wi-Fi hyperdrive).")
            debug_log.warn("warmup", "MQTT not connected after warmup window")
        except Exception as exc:
            print(f"Auto MQTT warmup skipped: {exc}")
            debug_log.exception("warmup", "warmup failed", exc)

    threading.Thread(target=_run, name="mqtt-warmup", daemon=True).start()


if __name__ == "__main__":
    debug_log.session_banner(
        {
            "callerId": orch.caller_id,
            "broker": f"{orch.broker_host}:{orch.broker_port}",
        }
    )
    print("Hyperdrive Remote -> http://127.0.0.1:5050")
    print(f"Caller ID (auto) = {orch.caller_id}")
    print(f"Debug log = {debug_log.log_path()}")
    print("Join Wi-Fi SSID 'hyperdrive' to reach the lab broker.")
    _warmup_lab_connection()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False, threaded=True)

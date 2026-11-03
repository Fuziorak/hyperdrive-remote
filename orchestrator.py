"""
Event-driven MQTT orchestrator for Hyperdrive / Anki Overdrive.

No hardcoded vehicle bindings: discovery, connect and control intents are
published as messages; feedback arrives as MQTT events and is forwarded to
the UI via registered listeners.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

import debug_log
from mqtt_util import mqtt_rc_ok, mqtt_rc_str


Listener = Callable[[dict[str, Any]], None]

LIGHT_CHANNELS = (
    "frontGreen",
    "frontRed",
    "tail",
    "engineRed",
    "engineGreen",
    "engineBlue",
)

LIGHT_EFFECTS = ("off", "steady", "fade", "pulse", "flash", "strobe")


def _default_light(effect: str = "off") -> dict[str, Any]:
    return {"effect": effect, "start": 0, "end": 15, "frequency": 10}


@dataclass
class VehicleState:
    vehicle_id: str
    model: str | None = None
    rssi: int | None = None
    connected: bool = False
    status: str = "unknown"
    on_track: bool | None = None
    battery_level: int | None = None
    charging: bool | None = None
    speed: int | None = None
    last_seen: float = field(default_factory=time.time)


class HyperdriveOrchestrator:
    """
    Bridges UI intents to Anki MQTT topics and fans events back out.

    Architecture (course requirement):
    - UI never talks MQTT directly
    - Orchestrator owns subscriptions dynamically (no hardcoding of vehicles)
    - Commands are intents published on Anki/.../I/{callerID}
    """

    def __init__(
        self,
        broker_host: str = "10.42.0.1",
        broker_port: int = 1883,
        host_id: str = "1",
        caller_id: str | None = None,
        mqtt_username: str = "Your_mom",
        mqtt_password: str = "",
    ) -> None:
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.host_id = host_id
        self.caller_id = caller_id or f"rc-{uuid.uuid4().hex[:8]}"
        # Lab broker (same profile as MQTT Explorer "University")
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password

        self._client: mqtt.Client | None = None
        self._lock = threading.RLock()
        self._connected = False
        self._connecting = False
        self._connect_started_at = 0.0
        self._scanning = False
        self._vehicles: dict[str, VehicleState] = {}
        self._listeners: list[Listener] = []
        self._subscribed_vehicles: set[str] = set()
        # Desired control state per vehicle (for re-assert on a shared network)
        self._desired: dict[str, dict[str, Any]] = {}
        self._hold_enabled: dict[str, bool] = {}
        self._hold_stop = threading.Event()
        self._hold_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ public

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mqttConnected": self._connected,
                "broker": f"{self.broker_host}:{self.broker_port}",
                "callerId": self.caller_id,
                "hostId": self.host_id,
                "mqttUsername": self.mqtt_username,
                "hasMqttPassword": bool(self.mqtt_password),
                "scanning": self._scanning,
                "hold": dict(self._hold_enabled),
                "desired": {vid: dict(cfg) for vid, cfg in self._desired.items()},
                "vehicles": {
                    vid: {
                        "id": v.vehicle_id,
                        "model": v.model,
                        "rssi": v.rssi,
                        "connected": v.connected,
                        "status": v.status,
                        "onTrack": v.on_track,
                        "batteryLevel": v.battery_level,
                        "charging": v.charging,
                        "speed": v.speed,
                        "lastSeen": v.last_seen,
                    }
                    for vid, v in self._vehicles.items()
                },
            }

    def _remember_desired(self, vehicle_id: str, **fields: Any) -> None:
        with self._lock:
            cfg = self._desired.setdefault(vehicle_id, {})
            for key, value in fields.items():
                if value is not None:
                    cfg[key] = value

    def connect_broker(self, force: bool = False) -> None:
        # Important: a previous failed attempt may leave a dead client object.
        # But do NOT kill a client that is still handshaking — that was preventing
        # on_connect from ever succeeding on the lab network.
        reachable = self.broker_reachable()
        age = time.time() - self._connect_started_at if self._connect_started_at else None
        stale = bool(
            self._client is not None
            and not self._connected
            and age is not None
            and age > 20.0
        )
        debug_log.info(
            "mqtt",
            "connect_broker called",
            force=force,
            broker=f"{self.broker_host}:{self.broker_port}",
            alreadyConnected=self._connected,
            hasClient=self._client is not None,
            connecting=self._connecting,
            connectAgeS=round(age, 2) if age is not None else None,
            staleHandshake=stale,
            tcpReachable=reachable,
            mqttUser=self.mqtt_username or None,
            callerId=self.caller_id,
            hostId=self.host_id,
        )
        if self._connected:
            debug_log.info("mqtt", "connect_broker skipped (already connected)")
            return
        if self._client is not None and not force and not stale:
            if self._connecting and age is not None and age <= 20.0:
                debug_log.info("mqtt", "connect_broker skipped (handshake already in progress)")
                return
            # Client exists but is neither connected nor actively connecting — retry.
            debug_log.warn("mqtt", "dropping idle/failed MQTT client before retry")
            self.disconnect_broker()
        elif self._client is not None and (force or stale):
            if stale:
                debug_log.warn("mqtt", "stale handshake — dropping client and retrying")
            elif force and not reachable:
                debug_log.warn("mqtt", "force reconnect — TCP dead, dropping client")
            elif force and reachable and self._connecting and age is not None and age <= 20.0:
                debug_log.warn(
                    "mqtt",
                    "force requested but handshake still young — keeping current client",
                )
                return
            else:
                debug_log.warn("mqtt", "dropping previous MQTT client before reconnect")
            self.disconnect_broker()

        client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"hyperdrive-remote-{self.caller_id}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
            reconnect_on_failure=False,
        )
        if self.mqtt_username:
            # Empty password → omit (MQTT Explorer has user, no password field).
            pw = self.mqtt_password if self.mqtt_password else None
            client.username_pw_set(self.mqtt_username, pw)
            debug_log.info("mqtt", "auth configured", username=self.mqtt_username, passwordSet=bool(pw))
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # Extra diagnostics when CONNACK never arrives
        if hasattr(client, "on_connect_fail"):
            client.on_connect_fail = self._on_connect_fail  # type: ignore[attr-defined]
        client.on_socket_open = self._on_socket_open  # type: ignore[method-assign]
        client.on_socket_close = self._on_socket_close  # type: ignore[method-assign]

        self._client = client
        self._connecting = True
        self._connect_started_at = time.time()

        def _connect_worker() -> None:
            try:
                debug_log.info(
                    "mqtt",
                    "blocking connect start",
                    broker=f"{self.broker_host}:{self.broker_port}",
                    username=self.mqtt_username or None,
                )
                # Order matters: TCP+CONNECT first, then pump until CONNACK, then loop_start.
                # loop_start-before-connect left us with TCP OK but no on_connect on the lab broker.
                client.connect(self.broker_host, self.broker_port, keepalive=30)
                debug_log.info("mqtt", "TCP+CONNECT sent — waiting CONNACK")
                deadline = time.time() + 12.0
                while (
                    time.time() < deadline
                    and self._client is client
                    and not self._connected
                    and self._connecting
                ):
                    client.loop(timeout=0.25)
                if self._connected and self._client is client:
                    client.reconnect_delay_set(min_delay=1, max_delay=30)
                    client.loop_start()
                    debug_log.info("mqtt", "CONNACK OK — background loop started")
                elif self._client is client:
                    debug_log.error(
                        "mqtt",
                        "CONNACK timeout — broker accepted TCP but no MQTT handshake",
                        username=self.mqtt_username or None,
                    )
                    self._connecting = False
                    self._emit(
                        {
                            "type": "mqttError",
                            "message": "CONNACK timeout (TCP OK, MQTT handshake failed — check user/password)",
                        }
                    )
                    self.disconnect_broker()
            except Exception as exc:
                debug_log.exception("mqtt", "blocking connect failed", exc)
                self._connected = False
                self._connecting = False
                self._emit({"type": "mqttError", "message": f"connect exception: {exc}"})
                if self._client is client:
                    self.disconnect_broker()

        threading.Thread(target=_connect_worker, name="mqtt-connect", daemon=True).start()

    def disconnect_broker(self) -> None:
        if self._client is None:
            self._connecting = False
            return
        debug_log.info("mqtt", "disconnect_broker")
        client = self._client
        self._client = None
        self._connected = False
        self._connecting = False
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def broker_reachable(self, timeout_s: float = 1.5) -> bool:
        """Quick TCP probe to the MQTT broker (does not require a full MQTT handshake)."""
        import socket

        try:
            with socket.create_connection(
                (self.broker_host, self.broker_port), timeout=timeout_s
            ):
                return True
        except OSError as exc:
            debug_log.warn(
                "mqtt",
                "TCP probe failed",
                broker=f"{self.broker_host}:{self.broker_port}",
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def configure(
        self,
        broker_host: str | None = None,
        broker_port: int | None = None,
        host_id: str | None = None,
        mqtt_username: str | None = None,
        mqtt_password: str | None = None,
    ) -> None:
        changed = False
        if broker_host and broker_host != self.broker_host:
            self.broker_host = broker_host
            changed = True
        if broker_port and broker_port != self.broker_port:
            self.broker_port = broker_port
            changed = True
        if host_id and host_id != self.host_id:
            self.host_id = host_id
            changed = True
        if mqtt_username is not None and mqtt_username != self.mqtt_username:
            self.mqtt_username = mqtt_username
            changed = True
        if mqtt_password is not None and mqtt_password != self.mqtt_password:
            self.mqtt_password = mqtt_password
            changed = True
        if changed and self._client is not None:
            self.disconnect_broker()
            self.connect_broker(force=True)

    # ---------------------------------------------------------------- intents

    def discover(self, enable: bool = True) -> None:
        self._scanning = enable
        self._publish_host_intent({"type": "discover", "payload": {"value": enable}})
        self._emit({"type": "scanning", "value": enable})

    def register_vehicle_manual(self, vehicle_id: str, model: str | None = None) -> None:
        vehicle_id = vehicle_id.strip()
        if not vehicle_id:
            raise ValueError("vehicle_id is required")
        with self._lock:
            vehicle = self._vehicles.get(vehicle_id)
            if vehicle is None:
                vehicle = VehicleState(vehicle_id=vehicle_id, model=model or "manual")
                self._vehicles[vehicle_id] = vehicle
            else:
                if model:
                    vehicle.model = model
                vehicle.last_seen = time.time()
        self._ensure_vehicle_subscriptions(vehicle_id)
        self._emit(
            {
                "type": "vehicleDiscovered",
                "vehicleId": vehicle_id,
                "model": model or "manual",
                "rssi": None,
                "manual": True,
            }
        )

    def connect_vehicle(self, vehicle_id: str, enable: bool = True) -> None:
        self._ensure_vehicle_subscriptions(vehicle_id)
        self._publish_vehicle_intent(
            vehicle_id,
            {"type": "connect", "payload": {"value": enable}},
        )

    def set_speed(
        self,
        vehicle_id: str,
        velocity: int,
        acceleration: int = 1000,
        *,
        remember: bool = True,
    ) -> None:
        velocity = max(-100, min(1000, int(velocity)))
        acceleration = max(0, min(2000, int(acceleration)))
        if remember:
            self._remember_desired(
                vehicle_id, velocity=velocity, acceleration=acceleration
            )
        self._publish_vehicle_intent(
            vehicle_id,
            {
                "type": "speed",
                "payload": {"velocity": velocity, "acceleration": acceleration},
            },
        )

    def set_lane(
        self,
        vehicle_id: str,
        offset: float,
        velocity: int = 300,
        acceleration: int = 800,
        offset_from_center: float | None = None,
        *,
        remember: bool = True,
    ) -> None:
        if offset_from_center is None:
            offset_from_center = offset
        payload = {
            "velocity": max(0, min(1000, int(velocity))),
            "acceleration": max(0, min(1000, int(acceleration))),
            "offset": float(offset),
            "offsetFromCenter": float(offset_from_center),
        }
        if remember:
            self._remember_desired(vehicle_id, lane=payload)
        self._publish_vehicle_intent(
            vehicle_id,
            {"type": "lane", "payload": payload},
        )

    def cancel_lane(self, vehicle_id: str) -> None:
        self._publish_vehicle_intent(
            vehicle_id,
            {"type": "cancelLane", "payload": {"value": True}},
        )

    def set_lights(
        self,
        vehicle_id: str,
        lights: dict[str, Any],
        *,
        remember: bool = True,
    ) -> None:
        payload: dict[str, Any] = {}
        for channel in LIGHT_CHANNELS:
            if channel not in lights:
                continue
            cfg = lights[channel] or {}
            effect = cfg.get("effect", "off")
            if effect not in LIGHT_EFFECTS:
                effect = "off"
            payload[channel] = {
                "effect": effect,
                "start": int(cfg.get("start", 0)),
                "end": int(cfg.get("end", 15)),
                "frequency": int(cfg.get("frequency", 10)),
            }
        if not payload:
            raise ValueError("at least one light channel is required")
        if remember:
            # Merge into previously remembered lights so partial updates keep the rest
            with self._lock:
                previous = dict((self._desired.get(vehicle_id) or {}).get("lights") or {})
            previous.update(payload)
            self._remember_desired(vehicle_id, lights=previous)
        self._publish_vehicle_intent(
            vehicle_id,
            {"type": "lights", "payload": payload},
        )

    def assert_control(self, vehicle_id: str) -> dict[str, Any]:
        """Re-publish remembered speed / lights / lane to override other clients."""
        with self._lock:
            desired = dict(self._desired.get(vehicle_id) or {})
        if not desired:
            raise ValueError("aucun réglage mémorisé pour ce véhicule — change vitesse/feux d'abord")

        applied: dict[str, Any] = {}
        if "velocity" in desired:
            self.set_speed(
                vehicle_id,
                desired.get("velocity", 0),
                desired.get("acceleration", 1000),
                remember=False,
            )
            applied["speed"] = {
                "velocity": desired.get("velocity", 0),
                "acceleration": desired.get("acceleration", 1000),
            }
        if desired.get("lights"):
            self.set_lights(vehicle_id, desired["lights"], remember=False)
            applied["lights"] = desired["lights"]
        if desired.get("lane"):
            lane = desired["lane"]
            self.set_lane(
                vehicle_id,
                offset=lane.get("offset", 0),
                velocity=lane.get("velocity", 300),
                acceleration=lane.get("acceleration", 800),
                offset_from_center=lane.get("offsetFromCenter"),
                remember=False,
            )
            applied["lane"] = lane

        self._emit({"type": "assertControl", "vehicleId": vehicle_id, "applied": applied})
        return applied

    def set_hold(self, vehicle_id: str, enabled: bool, interval_s: float = 1.5) -> None:
        """While enabled, periodically re-assert desired settings on the shared MQTT bus."""
        with self._lock:
            self._hold_enabled[vehicle_id] = bool(enabled)
            any_hold = any(self._hold_enabled.values())

        if enabled:
            self._start_hold_loop(interval_s)
        elif not any_hold:
            self._hold_stop.set()

        self._emit(
            {
                "type": "holdChanged",
                "vehicleId": vehicle_id,
                "enabled": bool(enabled),
                "interval": interval_s,
            }
        )

    def _start_hold_loop(self, interval_s: float) -> None:
        if self._hold_thread and self._hold_thread.is_alive():
            return
        self._hold_stop.clear()

        def _loop() -> None:
            while not self._hold_stop.wait(interval_s):
                with self._lock:
                    targets = [vid for vid, on in self._hold_enabled.items() if on]
                for vid in targets:
                    try:
                        self.assert_control(vid)
                    except Exception:
                        pass
                with self._lock:
                    if not any(self._hold_enabled.values()):
                        break

        self._hold_thread = threading.Thread(target=_loop, name="hold-control", daemon=True)
        self._hold_thread.start()

    def set_tail_flash(self, vehicle_id: str, enable: bool = True) -> None:
        """Convenience for the ethical-hack flag: flashing tail lights."""
        effect = "flash" if enable else "off"
        self.set_lights(
            vehicle_id,
            {
                "tail": {
                    "effect": effect,
                    "start": 0,
                    "end": 15,
                    "frequency": 20 if enable else 0,
                }
            },
        )

    def apply_light_preset(self, vehicle_id: str, preset: str) -> None:
        presets: dict[str, dict[str, Any]] = {
            "off": {ch: _default_light("off") for ch in LIGHT_CHANNELS},
            "green": {
                "engineGreen": _default_light("steady"),
                "frontGreen": _default_light("steady"),
                "engineRed": _default_light("off"),
                "engineBlue": _default_light("off"),
                "frontRed": _default_light("off"),
                "tail": _default_light("off"),
            },
            "red": {
                "engineRed": _default_light("steady"),
                "frontRed": _default_light("steady"),
                "engineGreen": _default_light("off"),
                "engineBlue": _default_light("off"),
                "frontGreen": _default_light("off"),
                "tail": _default_light("steady"),
            },
            "blue": {
                "engineBlue": _default_light("steady"),
                "engineRed": _default_light("off"),
                "engineGreen": _default_light("off"),
                "frontRed": _default_light("off"),
                "frontGreen": _default_light("off"),
                "tail": _default_light("off"),
            },
            "police": {
                "engineRed": {"effect": "flash", "start": 0, "end": 15, "frequency": 18},
                "engineBlue": {"effect": "flash", "start": 0, "end": 15, "frequency": 18},
                "frontRed": {"effect": "pulse", "start": 0, "end": 15, "frequency": 12},
                "frontGreen": _default_light("off"),
                "engineGreen": _default_light("off"),
                "tail": {"effect": "flash", "start": 0, "end": 15, "frequency": 14},
            },
            "tailFlash": {
                "tail": {"effect": "flash", "start": 0, "end": 15, "frequency": 20},
            },
            "strobe": {ch: {"effect": "strobe", "start": 0, "end": 15, "frequency": 30} for ch in LIGHT_CHANNELS},
        }
        if preset not in presets:
            raise ValueError(f"unknown preset: {preset}")
        self.set_lights(vehicle_id, presets[preset])
        self._remember_desired(vehicle_id, lightPreset=preset)

    def panic_stop(self, vehicle_id: str) -> None:
        self.set_speed(vehicle_id, velocity=0, acceleration=2000)

    # --------------------------------------------------------------- internals

    def _emit(self, event: dict[str, Any]) -> None:
        event = {**event, "ts": time.time()}
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._client is None or not self._connected:
            debug_log.error(
                "mqtt",
                "publish blocked: not connected",
                topic=topic,
                payload=payload,
            )
            raise RuntimeError("MQTT broker not connected — join Wi-Fi 'hyperdrive' first")
        body = json.dumps(payload)
        info = self._client.publish(topic, body, qos=1)
        debug_log.info(
            "mqtt",
            "publish",
            topic=topic,
            payload=payload,
            rc=getattr(info, "rc", None),
        )
        self._emit({"type": "mqttOut", "topic": topic, "payload": payload})

    def _publish_host_intent(self, intent: dict[str, Any]) -> None:
        topic = f"Anki/Hosts/U/{self.host_id}/I/{self.caller_id}"
        self._publish(topic, intent)

    def _publish_vehicle_intent(self, vehicle_id: str, intent: dict[str, Any]) -> None:
        self._ensure_vehicle_subscriptions(vehicle_id)
        topic = f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}"
        self._publish(topic, intent)

    def _ensure_vehicle_subscriptions(self, vehicle_id: str) -> None:
        if self._client is None or vehicle_id in self._subscribed_vehicles:
            return
        patterns = [
            f"Anki/Vehicles/U/{vehicle_id}/S/#",
            f"Anki/Vehicles/U/{vehicle_id}/E/#",
        ]
        for pattern in patterns:
            self._client.subscribe(pattern, qos=1)
        self._subscribed_vehicles.add(vehicle_id)
        self._emit(
            {
                "type": "subscription",
                "vehicleId": vehicle_id,
                "topics": patterns,
            }
        )

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        try:
            ok = mqtt_rc_ok(reason_code)
            self._connected = ok
            self._connecting = False
            debug_log.info(
                "mqtt",
                "on_connect",
                ok=ok,
                reason=mqtt_rc_str(reason_code),
                flags=str(flags),
                broker=f"{self.broker_host}:{self.broker_port}",
                username=self.mqtt_username or None,
            )
            if not ok:
                debug_log.error("mqtt", "MQTT connect failed", reason=mqtt_rc_str(reason_code))
                self._emit({"type": "mqttError", "message": f"connect failed: {reason_code}"})
                return

            # Host-level dynamic subscriptions (discovery + host status)
            host_topics = [
                f"Anki/Hosts/U/{self.host_id}/S/#",
                f"Anki/Hosts/U/{self.host_id}/E/#",
                "Anki/Hosts/U/+/E/vehicle/discovered/+",
                "Anki/Hosts/+/E/vehicle/discovered/+",
            ]
            for topic in host_topics:
                client.subscribe(topic, qos=1)
            debug_log.info("mqtt", "subscribed host topics", topics=host_topics)

            self._emit({"type": "mqttConnected", "broker": f"{self.broker_host}:{self.broker_port}"})
        except Exception as exc:
            debug_log.exception("mqtt", "on_connect handler crashed", exc)
            self._connected = False
            self._connecting = False
            self._emit({"type": "mqttError", "message": f"on_connect crash: {exc}"})

    def _on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        debug_log.error("mqtt", "on_connect_fail")
        self._connecting = False
        self._connected = False
        self._emit({"type": "mqttError", "message": "MQTT connect_fail callback"})

    def _on_socket_open(self, client: mqtt.Client, userdata: Any, sock: Any) -> None:
        debug_log.info("mqtt", "socket open", peer=str(getattr(sock, "getpeername", lambda: None)()))

    def _on_socket_close(self, client: mqtt.Client, userdata: Any, sock: Any) -> None:
        debug_log.warn("mqtt", "socket close")

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        self._connected = False
        self._connecting = False
        self._subscribed_vehicles.clear()
        debug_log.warn("mqtt", "on_disconnect", reason=mqtt_rc_str(reason_code))
        self._emit({"type": "mqttDisconnected", "reason": mqtt_rc_str(reason_code)})

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        topic = message.topic
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            payload = {"raw": message.payload.decode("utf-8", errors="replace")}

        debug_log.info("mqtt", "message in", topic=topic, payload=payload)
        self._emit({"type": "mqttIn", "topic": topic, "payload": payload})
        self._route_incoming(topic, payload)

    def _route_incoming(self, topic: str, payload: dict[str, Any]) -> None:
        parts = topic.split("/")

        # Anki/Hosts/.../E/vehicle/discovered/{vehicleID}
        if "discovered" in parts:
            try:
                idx = parts.index("discovered")
                vehicle_id = parts[idx + 1]
            except (ValueError, IndexError):
                return
            value = payload.get("value", payload)
            model = value.get("model") if isinstance(value, dict) else None
            rssi = value.get("rssi") if isinstance(value, dict) else None
            with self._lock:
                vehicle = self._vehicles.get(vehicle_id)
                if vehicle is None:
                    vehicle = VehicleState(vehicle_id=vehicle_id)
                    self._vehicles[vehicle_id] = vehicle
                vehicle.model = model or vehicle.model
                vehicle.rssi = rssi if rssi is not None else vehicle.rssi
                vehicle.last_seen = time.time()
            self._ensure_vehicle_subscriptions(vehicle_id)
            self._emit(
                {
                    "type": "vehicleDiscovered",
                    "vehicleId": vehicle_id,
                    "model": model,
                    "rssi": rssi,
                }
            )
            return

        if len(parts) >= 3 and parts[0] == "Anki" and parts[1] == "Vehicles":
            try:
                vehicle_id = parts[3]
            except IndexError:
                return
            with self._lock:
                vehicle = self._vehicles.get(vehicle_id)
                if vehicle is None:
                    vehicle = VehicleState(vehicle_id=vehicle_id)
                    self._vehicles[vehicle_id] = vehicle
                vehicle.last_seen = time.time()

                leaf = "/".join(parts[4:])
                value = payload.get("value", payload)

                if leaf.endswith("status") or leaf == "S/status":
                    vehicle.status = str(value)
                    vehicle.connected = str(value).lower() == "connected"
                elif "connection" in leaf:
                    vehicle.connected = bool(value) if not isinstance(value, dict) else bool(value)
                elif "onTrack" in leaf:
                    vehicle.on_track = bool(value)
                elif leaf.endswith("battery/level") or leaf.endswith("level"):
                    try:
                        vehicle.battery_level = int(value)
                    except Exception:
                        pass
                elif "charging" in leaf:
                    vehicle.charging = bool(value)
                elif leaf.endswith("E/speed") or leaf.endswith("/speed"):
                    try:
                        vehicle.speed = int(value)
                    except Exception:
                        pass

            self._emit(
                {
                    "type": "vehicleUpdate",
                    "vehicleId": vehicle_id,
                    "topic": topic,
                    "payload": payload,
                    "state": {
                        "connected": vehicle.connected,
                        "status": vehicle.status,
                        "onTrack": vehicle.on_track,
                        "batteryLevel": vehicle.battery_level,
                        "charging": vehicle.charging,
                        "speed": vehicle.speed,
                        "model": vehicle.model,
                        "rssi": vehicle.rssi,
                    },
                }
            )
            return

        if "scanning" in topic or topic.endswith("/discover"):
            value = payload.get("value", payload)
            self._scanning = bool(value)
            self._emit({"type": "scanning", "value": self._scanning})

"""
Minimal MQTT bridge — same settings as MQTT Explorer "University".

Goal: get a reliable CONNACK first. Everything else is secondary.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable

import paho.mqtt.client as mqtt

import debug_log
from mqtt_util import mqtt_rc_ok, mqtt_rc_str

Listener = Callable[[dict[str, Any]], None]

DEFAULT_HOST = "10.42.0.1"
DEFAULT_PORT = 1883
DEFAULT_USER = "Your_mom"
DEFAULT_PASSWORD = ""  # set in UI if MQTT Explorer uses one
# Lab host id observed on broker: Anki/Hosts/U/hyperdrive/...
DEFAULT_HOST_ID = "hyperdrive"


class MqttBridge:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        username: str = DEFAULT_USER,
        password: str = DEFAULT_PASSWORD,
        host_id: str = DEFAULT_HOST_ID,
        caller_id: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.host_id = host_id
        self.caller_id = caller_id or f"rc-{uuid.uuid4().hex[:8]}"

        self._client: mqtt.Client | None = None
        self._lock = threading.RLock()
        self._connected = False
        self._listeners: list[Listener] = []
        self._vehicles: dict[str, dict[str, Any]] = {}
        self._scanning = False
        self._desired: dict[str, dict[str, Any]] = {}
        self._hold_enabled: dict[str, bool] = {}
        self._hold_stop = threading.Event()
        self._hold_thread: threading.Thread | None = None
        self._subscribed_vehicles: set[str] = set()
        self._base_topics: list[str] = []
        self._reconnect_lock = threading.Lock()
        self._announced: set[str] = set()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._start_watchdog()

    # ----- public API used by app.py -----

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def broker_host(self) -> str:
        return self.host

    @property
    def broker_port(self) -> int:
        return self.port

    @property
    def mqtt_username(self) -> str:
        return self.username

    @property
    def mqtt_password(self) -> str:
        return self.password

    def configure(
        self,
        broker_host: str | None = None,
        broker_port: int | None = None,
        host_id: str | None = None,
        mqtt_username: str | None = None,
        mqtt_password: str | None = None,
    ) -> None:
        if broker_host:
            self.host = broker_host
        if broker_port:
            self.port = int(broker_port)
        if host_id:
            self.host_id = host_id
        if mqtt_username is not None:
            self.username = mqtt_username
        if mqtt_password is not None:
            self.password = mqtt_password

    def broker_reachable(self, timeout_s: float = 1.5) -> bool:
        import socket

        try:
            with socket.create_connection((self.host, self.port), timeout=timeout_s):
                return True
        except OSError as exc:
            debug_log.warn("mqtt", "TCP probe failed", error=f"{type(exc).__name__}: {exc}")
            return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mqttConnected": self._connected,
                "broker": f"{self.host}:{self.port}",
                "callerId": self.caller_id,
                "hostId": self.host_id,
                "mqttUsername": self.username,
                "hasMqttPassword": bool(self.password),
                "scanning": self._scanning,
                "hold": dict(self._hold_enabled),
                "desired": {k: dict(v) for k, v in self._desired.items()},
                "vehicles": dict(self._vehicles),
            }

    def connect_broker(self, force: bool = False) -> dict[str, Any]:
        """
        Synchronous-style connect in a worker:
        returns immediately to caller; result is pushed via events + _connected flag.
        """
        with self._lock:
            if self._connected and not force:
                debug_log.info("mqtt", "already connected")
                return {"ok": True, "already": True}
            if self._client is not None:
                self._drop_client_unlocked()

        threading.Thread(
            target=self._connect_sync,
            name="mqtt-connect",
            daemon=True,
        ).start()
        return {"ok": True, "started": True}

    def disconnect_broker(self) -> None:
        with self._lock:
            self._drop_client_unlocked()
        self._emit({"type": "mqttDisconnected", "reason": "client disconnect"})

    def _start_watchdog(self) -> None:
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return

        def _loop() -> None:
            while not self._watchdog_stop.wait(5.0):
                if self._connected:
                    continue
                # Only retry when lab Wi‑Fi actually reaches the broker.
                if not self.broker_reachable(timeout_s=1.2):
                    continue
                with self._reconnect_lock:
                    if self._connected:
                        continue
                    debug_log.warn(
                        "mqtt",
                        "watchdog: TCP OK but MQTT down — reconnecting",
                    )
                    try:
                        self.connect_broker(force=True)
                    except Exception as exc:
                        debug_log.exception("mqtt", "watchdog reconnect failed", exc)

        self._watchdog_thread = threading.Thread(target=_loop, name="mqtt-watchdog", daemon=True)
        self._watchdog_thread.start()

    def discover(self, enable: bool = True) -> None:
        """Publish HostIntent discover on Anki/Hosts/U/I/{callerID} (lab network scan)."""
        self._scanning = enable
        intent = {"type": "discover", "payload": {"value": bool(enable)}}
        # Global host intent — required to start BLE discovery on the lab hosts.
        # Topic shape from Hyperdrive API: Anki/Hosts/U/I/{callerID}
        topics = [
            f"Anki/Hosts/U/I/{self.caller_id}",
            f"Anki/Hosts/U/{self.host_id}/I/{self.caller_id}",
        ]
        for topic in topics:
            self._publish(topic, intent)
            debug_log.info("mqtt", "discover intent published", topic=topic, payload=intent)
        self._emit({"type": "scanning", "value": bool(enable)})

    def register_vehicle_manual(self, vehicle_id: str, model: str | None = None) -> None:
        vehicle_id = vehicle_id.strip()
        if not vehicle_id:
            raise ValueError("vehicle_id required")
        with self._lock:
            self._vehicles[vehicle_id] = {
                "id": vehicle_id,
                "model": model or "manual",
                "rssi": None,
                "connected": False,
                "status": "manual",
                "onTrack": None,
                "batteryLevel": None,
                "charging": None,
                "speed": None,
                "lastSeen": time.time(),
            }
        self._subscribe_vehicle(vehicle_id)
        self._emit({"type": "vehicleDiscovered", "vehicleId": vehicle_id, "model": model or "manual"})

    def connect_vehicle(self, vehicle_id: str, enable: bool = True) -> None:
        self._subscribe_vehicle(vehicle_id)
        self._remember(vehicle_id, connect=bool(enable))
        self._publish(f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}", {
            "type": "connect",
            "payload": {"value": enable},
        })

    def set_speed(self, vehicle_id: str, velocity: int, acceleration: int = 1000) -> None:
        self._remember(vehicle_id, velocity=velocity, acceleration=acceleration)
        self._publish(f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}", {
            "type": "speed",
            "payload": {"velocity": int(velocity), "acceleration": int(acceleration)},
        })

    def set_lane(
        self,
        vehicle_id: str,
        offset: float,
        velocity: int = 300,
        acceleration: int = 800,
        offset_from_center: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "velocity": int(velocity),
            "acceleration": int(acceleration),
            "offset": float(offset),
        }
        if offset_from_center is not None:
            payload["offsetFromCenter"] = float(offset_from_center)
        self._remember(vehicle_id, lane=payload)
        self._publish(f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}", {
            "type": "lane",
            "payload": payload,
        })

    def cancel_lane(self, vehicle_id: str) -> None:
        self._publish(f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}", {
            "type": "cancelLane",
            "payload": {"value": True},
        })

    def set_lights(self, vehicle_id: str, lights: dict[str, Any]) -> None:
        self._remember(vehicle_id, lights=lights)
        self._publish(f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}", {
            "type": "lights",
            "payload": lights,
        })

    def apply_light_preset(self, vehicle_id: str, preset: str) -> None:
        from orchestrator import LIGHT_CHANNELS, _default_light

        lights = {ch: _default_light("off") for ch in LIGHT_CHANNELS}
        p = (preset or "off").lower()
        if p == "green":
            lights["frontGreen"] = _default_light("steady")
            lights["engineGreen"] = _default_light("steady")
        elif p == "red":
            lights["frontRed"] = _default_light("steady")
            lights["engineRed"] = _default_light("steady")
            lights["tail"] = _default_light("steady")
        elif p == "blue":
            lights["engineBlue"] = _default_light("steady")
        elif p == "police":
            lights["frontRed"] = {"effect": "flash", "start": 0, "end": 15, "frequency": 20}
            lights["frontGreen"] = {"effect": "flash", "start": 0, "end": 15, "frequency": 20}
            lights["engineBlue"] = {"effect": "flash", "start": 0, "end": 15, "frequency": 15}
        elif p == "tailflash":
            lights["tail"] = {"effect": "flash", "start": 0, "end": 15, "frequency": 12}
        elif p == "strobe":
            for ch in LIGHT_CHANNELS:
                lights[ch] = {"effect": "strobe", "start": 0, "end": 15, "frequency": 30}
        self.set_lights(vehicle_id, lights)

    def panic_stop(self, vehicle_id: str) -> None:
        self.set_speed(vehicle_id, 0, 2000)

    def assert_control(self, vehicle_id: str) -> dict[str, Any]:
        cfg = self._desired.get(vehicle_id) or {}
        applied: dict[str, Any] = {}
        if cfg.get("connect"):
            self._publish(f"Anki/Vehicles/U/{vehicle_id}/I/{self.caller_id}", {
                "type": "connect",
                "payload": {"value": True},
            })
            applied["connect"] = True
        if "velocity" in cfg:
            self.set_speed(vehicle_id, cfg["velocity"], cfg.get("acceleration", 1000))
            applied["speed"] = True
        if "lane" in cfg:
            lane = cfg["lane"]
            self.set_lane(
                vehicle_id,
                offset=lane.get("offset", 0),
                velocity=lane.get("velocity", 300),
                acceleration=lane.get("acceleration", 800),
                offset_from_center=lane.get("offsetFromCenter"),
            )
            applied["lane"] = True
        if "lights" in cfg:
            self.set_lights(vehicle_id, cfg["lights"])
            applied["lights"] = True
        self._emit({"type": "assertControl", "vehicleId": vehicle_id, "applied": applied})
        return applied

    def set_hold(self, vehicle_id: str, enabled: bool, interval_s: float = 1.5) -> None:
        self._hold_enabled[vehicle_id] = enabled
        if enabled and (self._hold_thread is None or not self._hold_thread.is_alive()):
            self._hold_stop.clear()
            self._hold_thread = threading.Thread(target=self._hold_loop, args=(interval_s,), daemon=True)
            self._hold_thread.start()
        if not any(self._hold_enabled.values()):
            self._hold_stop.set()
        self._emit({"type": "holdChanged", "vehicleId": vehicle_id, "enabled": enabled})

    # ----- internals -----

    def _connect_sync(self) -> None:
        client_id = f"hyperdrive-remote-{self.caller_id}"
        debug_log.info(
            "mqtt",
            "connect_sync start",
            broker=f"{self.host}:{self.port}",
            username=self.username or None,
            clientId=client_id,
        )

        # Match MQTT Explorer: mqtt:// host:1883, optional user/pass, no TLS
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
            reconnect_on_failure=True,
        )
        client.reconnect_delay_set(min_delay=1, max_delay=20)
        # MQTT Explorer profile has username but NO password field → do not send empty password.
        if self.username:
            pw = self.password if self.password else None
            client.username_pw_set(self.username, pw)
            debug_log.info("mqtt", "auth set", username=self.username, passwordSet=bool(pw))

        connected = threading.Event()
        failed_reason: list[str] = []
        first_connect = {"done": False}

        def on_connect(c, userdata, flags, reason_code, properties=None):
            ok = mqtt_rc_ok(reason_code)
            debug_log.info(
                "mqtt",
                "on_connect",
                ok=ok,
                reason=mqtt_rc_str(reason_code),
                flags=str(flags),
                sessionPresent=getattr(flags, "session_present", None),
            )
            if not ok:
                failed_reason.append(mqtt_rc_str(reason_code))
                connected.set()
                return
            self._connected = True
            connected.set()
            try:
                self._apply_subscriptions(c)
            except Exception as exc:
                debug_log.exception("mqtt", "subscribe after connect failed", exc)
            # Always start host discovery as soon as MQTT is up (server boot + reconnect).
            def _boot_discover() -> None:
                try:
                    time.sleep(0.15)
                    self.discover(True)
                    debug_log.info("mqtt", "boot discover sent on Anki/Hosts/U/I/{callerId}")
                except Exception as exc:
                    debug_log.exception("mqtt", "boot discover failed", exc)
                self._reassert_all()

            threading.Thread(target=_boot_discover, name="mqtt-boot-discover", daemon=True).start()
            if first_connect["done"]:
                debug_log.info("mqtt", "auto-reconnected — discover + hold restored")
            first_connect["done"] = True
            self._emit({"type": "mqttConnected", "broker": f"{self.host}:{self.port}"})

        def on_disconnect(c, userdata, flags, reason_code, properties=None):
            debug_log.warn("mqtt", "on_disconnect", reason=mqtt_rc_str(reason_code))
            self._connected = False
            self._emit({"type": "mqttDisconnected", "reason": mqtt_rc_str(reason_code)})

        def on_message(c, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                payload = {"raw": msg.payload.decode("utf-8", errors="replace")}
            # Do not flood UI/debug with every battery tick — route only.
            self._route(msg.topic, payload)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        with self._lock:
            self._client = client

        try:
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
            if not connected.wait(timeout=12):
                debug_log.error("mqtt", "CONNACK timeout")
                self._emit({"type": "mqttError", "message": "CONNACK timeout (12s) — vérifie Wi-Fi hyperdrive + user/password"})
                self.disconnect_broker()
                return
            if failed_reason:
                msg = f"MQTT rejected: {failed_reason[0]}"
                debug_log.error("mqtt", msg)
                self._emit({"type": "mqttError", "message": msg})
                self.disconnect_broker()
                return
        except Exception as exc:
            debug_log.exception("mqtt", "connect_sync failed", exc)
            self._emit({"type": "mqttError", "message": f"connect failed: {exc}"})
            self.disconnect_broker()

    def _drop_client_unlocked(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload)
        client = self._client
        if not client or not self._connected:
            raise RuntimeError("MQTT not connected")
        client.publish(topic, raw, qos=1)
        self._emit({"type": "mqttOut", "topic": topic, "payload": payload})

    def _subscribe_vehicle(self, vehicle_id: str) -> None:
        if vehicle_id in self._subscribed_vehicles:
            return
        client = self._client
        if not client or not self._connected:
            return
        # Avoid Anki/Vehicles/U/+/S/# global flood (battery Hz) — selective topics only.
        patterns = [
            f"Anki/Vehicles/U/{vehicle_id}/S/online",
            f"Anki/Vehicles/U/{vehicle_id}/S/status",
            f"Anki/Vehicles/U/{vehicle_id}/S/onTrack",
            f"Anki/Vehicles/U/{vehicle_id}/S/battery/level",
            f"Anki/Vehicles/U/{vehicle_id}/E/#",
        ]
        for t in patterns:
            client.subscribe(t, qos=1)
        self._subscribed_vehicles.add(vehicle_id)
        self._emit({"type": "subscription", "vehicleId": vehicle_id, "topics": patterns})

    def _remember(self, vehicle_id: str, **fields: Any) -> None:
        cfg = self._desired.setdefault(vehicle_id, {})
        for k, v in fields.items():
            if v is not None:
                cfg[k] = v

    def _hold_loop(self, interval_s: float) -> None:
        while not self._hold_stop.wait(interval_s):
            if not self._connected:
                continue
            for vid, on in list(self._hold_enabled.items()):
                if on:
                    try:
                        self.assert_control(vid)
                    except Exception as exc:
                        debug_log.warn("mqtt", "hold assert failed", vehicleId=vid, error=str(exc))

    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def _base_subscribe_topics(self) -> list[str]:
        # Narrow filters: full Vehicles/+/S/# caused MQTT drops (Unspecified error)
        # while short test_mqtt (3s) still succeeded.
        return [
            "Anki/Hosts/U/+/E/#",
            "Anki/Hosts/U/+/S/online",
            "Anki/Hosts/U/+/S/discover",
            "Anki/Hosts/U/+/E/vehicle/discovered/+",
            "Anki/Vehicles/U/+/S/online",
            "Anki/Vehicles/U/+/S/status",
        ]

    def _apply_subscriptions(self, client: mqtt.Client) -> None:
        self._base_topics = self._base_subscribe_topics()
        for t in self._base_topics:
            client.subscribe(t, qos=1)
        # Re-subscribe per-vehicle patterns after reconnect
        self._subscribed_vehicles.clear()
        for vid in list(self._vehicles.keys()):
            self._subscribe_vehicle(vid)
        debug_log.info("mqtt", "subscribed", topics=self._base_topics)

    def _reassert_all(self) -> None:
        for vid, on in list(self._hold_enabled.items()):
            if on:
                try:
                    self.assert_control(vid)
                except Exception as exc:
                    debug_log.warn("mqtt", "reassert failed", vehicleId=vid, error=str(exc))

    def _upsert_vehicle(
        self,
        vid: str,
        *,
        model: str | None = None,
        rssi: int | None = None,
        status: str | None = None,
        announce: bool = False,
    ) -> dict[str, Any]:
        if vid.upper().startswith("SIM"):
            return {}
        with self._lock:
            is_new = vid not in self._vehicles
            prev = self._vehicles.get(vid) or {
                "id": vid,
                "connected": False,
                "status": "discovered",
                "model": None,
                "rssi": None,
                "onTrack": None,
                "batteryLevel": None,
                "charging": None,
                "speed": None,
            }
            prev["id"] = vid
            prev["lastSeen"] = time.time()
            if model:
                prev["model"] = model
            if rssi is not None:
                prev["rssi"] = rssi
            if status:
                prev["status"] = status
            self._vehicles[vid] = prev
            snap = dict(prev)
        # Announce only once — battery spam was flooding WS + killing responsiveness.
        if is_new and vid not in self._announced:
            self._announced.add(vid)
            self._subscribe_vehicle(vid)
            self._emit({
                "type": "vehicleDiscovered",
                "vehicleId": vid,
                "model": snap.get("model"),
                "rssi": snap.get("rssi"),
            })
        elif announce and vid not in self._announced:
            self._announced.add(vid)
            self._subscribe_vehicle(vid)
            self._emit({
                "type": "vehicleDiscovered",
                "vehicleId": vid,
                "model": snap.get("model"),
                "rssi": snap.get("rssi"),
            })
        return snap

    def _route(self, topic: str, payload: dict[str, Any]) -> None:
        parts = topic.split("/")

        if "discovered" in parts:
            try:
                idx = parts.index("discovered")
                vid = parts[idx + 1]
            except (ValueError, IndexError):
                return
            value = payload.get("value") if isinstance(payload, dict) else None
            model = None
            rssi = None
            if isinstance(value, dict):
                model = value.get("model")
                rssi = value.get("rssi")
            self._upsert_vehicle(vid, model=model, rssi=rssi, announce=True)
            return

        # Any traffic under Anki/Vehicles/U/{vehicleId}/… ⇒ vehicle exists
        if len(parts) >= 4 and parts[0] == "Anki" and parts[1] == "Vehicles" and parts[2] == "U":
            vid = parts[3]
            if not vid or vid in {"I", "D"}:
                return
            self._upsert_vehicle(vid)
            with self._lock:
                v = self._vehicles.setdefault(vid, {"id": vid, "connected": False, "status": "unknown"})
                v["lastSeen"] = time.time()
                leaf = parts[-1]
                parent = parts[-2] if len(parts) >= 2 else ""
                val = payload.get("value") if isinstance(payload, dict) else None
                changed = False
                if leaf == "connection" or parent == "connection":
                    if isinstance(val, bool):
                        v["connected"] = val
                        v["status"] = "connected" if val else "disconnected"
                        changed = True
                elif leaf == "status":
                    if isinstance(val, str):
                        v["status"] = val
                        v["connected"] = val == "connected"
                        changed = True
                elif leaf == "level" and "battery" in parts:
                    if isinstance(val, (int, float)):
                        v["batteryLevel"] = int(val)
                        changed = True
                elif leaf == "charging":
                    if isinstance(val, bool):
                        v["charging"] = val
                        changed = True
                elif leaf == "onTrack":
                    if isinstance(val, bool):
                        v["onTrack"] = val
                        changed = True
                elif leaf == "speed" and parent == "E":
                    if isinstance(val, (int, float)):
                        v["speed"] = int(val)
                        changed = True
                elif leaf == "rssi":
                    if isinstance(val, (int, float)):
                        v["rssi"] = int(val)
                        changed = True
                snap = dict(v)
            # Only push UI updates for meaningful telemetry (not every battery tick flood via WS logs)
            if changed and leaf in {"status", "connection", "speed", "onTrack", "level", "rssi"}:
                self._emit({"type": "vehicleUpdate", "vehicleId": vid, "state": snap})

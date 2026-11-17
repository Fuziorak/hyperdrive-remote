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
from track_map import TrackController

Listener = Callable[[dict[str, Any]], None]

DEFAULT_HOST = "10.42.0.1"
DEFAULT_PORT = 1883
DEFAULT_USER = "Your_mom"
DEFAULT_PASSWORD = ""  # set in UI if MQTT Explorer uses one
# Lab host id observed on broker: Anki/Hosts/U/hyperdrive/...
DEFAULT_HOST_ID = "hyperdrive"
# Optional robustness mode: republish last speed while hold checkbox is on.
_HOLD_INTERVAL_S = 0.35


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
        self._want_connected = False
        self._connecting = False
        self._connect_started_at = 0.0
        self._reconnect_failures = 0
        self._reconnect_pending = False
        self.track = TrackController(self._emit)
        self._race_brake_timer: threading.Timer | None = None
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

    def broker_reachable(self, timeout_s: float = 0.8) -> bool:
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
                "track": self.track.snapshot(),
            }

    def connect_broker(self, force: bool = False) -> dict[str, Any]:
        """
        Same auth/path as test_mqtt.py (validated yesterday on lab Wi‑Fi).
        Avoid killing an in-flight handshake — that caused phantom 'disconnected' UI.
        """
        self._want_connected = True
        with self._lock:
            if self._connected and not force:
                debug_log.info("mqtt", "already connected")
                return {"ok": True, "already": True}
            age = time.time() - self._connect_started_at if self._connect_started_at else 999
            if self._connecting and age < 20 and not force:
                debug_log.info("mqtt", "connect already in progress", ageS=round(age, 1))
                return {"ok": True, "in_progress": True}
            if self._client is not None and self._connecting and age < 20 and force:
                debug_log.info("mqtt", "force ignored — handshake still young", ageS=round(age, 1))
                return {"ok": True, "in_progress": True}
            to_drop = None
            if self._client is not None:
                debug_log.warn("mqtt", "dropping MQTT client before new connect")
                to_drop = self._detach_client_unlocked()
            self._connecting = True
            self._connect_started_at = time.time()

        # Never loop_stop/disconnect under _lock — that blocked /api/state & UI boot.
        self._close_client(to_drop)

        threading.Thread(
            target=self._connect_sync,
            name="mqtt-connect",
            daemon=True,
        ).start()
        return {"ok": True, "started": True}

    def disconnect_broker(self) -> None:
        self._want_connected = False
        with self._lock:
            to_drop = self._detach_client_unlocked()
        self._close_client(to_drop)
        self._emit({"type": "mqttDisconnected", "reason": "client disconnect"})

    def _schedule_reconnect(self, delay_s: float | None = None) -> None:
        """Recreate client after drop — single flight + exponential backoff when TCP is down."""
        with self._reconnect_lock:
            if self._reconnect_pending:
                return
            self._reconnect_pending = True
            failures = self._reconnect_failures
        if delay_s is None:
            # 2, 4, 8, 16, 30s — stops hammering the broker after Unspecified error.
            delay_s = min(30.0, 2.0 * (2 ** min(failures, 4)))

        def _later() -> None:
            again = False
            try:
                time.sleep(delay_s)
                if not self._want_connected or self._connected:
                    return
                if not self.broker_reachable(timeout_s=1.5):
                    self._reconnect_failures += 1
                    debug_log.warn(
                        "mqtt",
                        "reconnect deferred — broker TCP down",
                        failures=self._reconnect_failures,
                        nextDelayS=min(30.0, 2.0 * (2 ** min(self._reconnect_failures, 4))),
                    )
                    again = True
                    return
                debug_log.info(
                    "mqtt",
                    "scheduled reconnect after drop",
                    failures=self._reconnect_failures,
                    delayS=delay_s,
                )
                self.connect_broker(force=True)
            finally:
                with self._reconnect_lock:
                    self._reconnect_pending = False
                if again and self._want_connected and not self._connected:
                    self._schedule_reconnect()

        threading.Thread(target=_later, name="mqtt-reconnect", daemon=True).start()

    def _start_watchdog(self) -> None:
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return

        def _loop() -> None:
            while not self._watchdog_stop.wait(10.0):
                if not self._want_connected or self._connected or self._connecting:
                    continue
                with self._reconnect_lock:
                    if self._reconnect_pending or self._connected or self._connecting:
                        continue
                if not self.broker_reachable(timeout_s=1.2):
                    continue
                debug_log.warn("mqtt", "watchdog: TCP OK, MQTT down — reconnect")
                self._schedule_reconnect(0.5)

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

    def set_speed(
        self,
        vehicle_id: str,
        velocity: int,
        acceleration: int = 1000,
        *,
        manual: bool = False,
    ) -> None:
        """Send speed once. Regular republish only if hold checkbox is enabled for this car.

        manual=True (cockpit / STOP) coupe course map, mapping et latence pour ce véhicule.
        """
        if manual:
            self.track.yield_to_manual(vehicle_id)
            self._cancel_race_brake()
        self._remember(vehicle_id, velocity=velocity, acceleration=acceleration)
        self._publish_speed(vehicle_id, int(velocity), int(acceleration))
        # STOP / vitesse 0 → couper la republication si elle était active.
        if int(velocity) <= 0 and self._hold_enabled.get(vehicle_id):
            self.set_hold(vehicle_id, False)

    def _cancel_race_brake(self) -> None:
        timer = self._race_brake_timer
        self._race_brake_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _apply_race_plan(self, vehicle_id: str, plan: dict[str, Any] | None) -> None:
        """Plein gaz en entrée de bout, puis frein programmé à 2/3 ou 3/4 du droit."""
        self._cancel_race_brake()
        if not plan:
            return
        entry = int(plan.get("entrySpeed") or 0)
        brake = int(plan.get("brakeSpeed") or entry)
        delay_ms = plan.get("brakeAfterMs")
        seg_idx = int(plan.get("segmentIndex", -1))
        self.set_speed(vehicle_id, entry, 1400, manual=False)
        if not delay_ms or brake >= entry:
            return

        def _fire() -> None:
            try:
                if not self.track.race_active:
                    return
                if int(self.track.current_segment) != seg_idx:
                    return
                if (self.track.race_vehicle or "").upper() != str(vehicle_id).upper():
                    return
                self.set_speed(vehicle_id, brake, 2000, manual=False)
                self._emit(
                    {
                        "type": "raceBrakeApplied",
                        "vehicleId": vehicle_id,
                        "segmentIndex": seg_idx,
                        "brakeSpeed": brake,
                        "brakeAfterMs": delay_ms,
                        "brakeAtFraction": plan.get("brakeAtFraction"),
                    }
                )
            except Exception as exc:
                debug_log.warn("mqtt", "race mid-brake failed", error=str(exc))

        timer = threading.Timer(float(delay_ms) / 1000.0, _fire)
        timer.daemon = True
        self._race_brake_timer = timer
        timer.start()

    def _publish_speed(self, vehicle_id: str, velocity: int, acceleration: int) -> None:
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
        *,
        manual: bool = False,
    ) -> None:
        if manual:
            self.track.yield_to_manual(vehicle_id)
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
        # STOP cockpit = priorité absolue sur course / mapping / latence
        self.set_speed(vehicle_id, 0, 2000, manual=True)

    def start_latency_test(self, vehicle_id: str, max_speed: int = 1000, accel: int = 2000) -> dict[str, Any]:
        self._subscribe_vehicle(vehicle_id)
        self.track.start_latency_test(vehicle_id)
        self.set_speed(vehicle_id, max_speed, accel)
        return {"ok": True, "vehicleId": vehicle_id}

    def start_mapping(
        self,
        vehicle_id: str,
        cruise_speed: int = 300,
        part_count: int | None = None,
    ) -> dict[str, Any]:
        self._subscribe_vehicle(vehicle_id)
        # Tracking d'abord ; set_speed en manual=False pour ne pas couper le mapping.
        out = self.track.start_mapping(vehicle_id, part_count=part_count)
        spd = max(0, min(300, int(cruise_speed)))
        speed_ok = True
        speed_error = None
        try:
            self.set_speed(vehicle_id, spd, 1000, manual=False)
        except Exception as exc:
            # Le mapping doit quand même démarrer même si le publish échoue un instant
            speed_ok = False
            speed_error = str(exc)
            debug_log.warn("mqtt", "mapping speed publish failed", error=speed_error, vehicleId=vehicle_id)
        out = {
            **out,
            "speed": spd,
            "speedOk": speed_ok,
            "speedError": speed_error,
            "mapping": True,
            "track": self.track.snapshot(),
        }
        return out

    def stop_mapping(self, finalize: bool = False) -> dict[str, Any]:
        vid = self.track.mapping_vehicle
        out = self.track.stop_mapping(finalize=finalize)
        if vid:
            try:
                # STOP physique sans ré-effacer via yield (déjà clear côté track)
                self._remember(vid, velocity=0, acceleration=2000)
                self._publish_speed(vid, 0, 2000)
                if self._hold_enabled.get(vid):
                    self.set_hold(vid, False)
            except Exception:
                pass
        return out

    def start_mapped_race(self, vehicle_id: str) -> dict[str, Any]:
        self._subscribe_vehicle(vehicle_id)
        out = self.track.start_race(vehicle_id)
        self._apply_race_plan(vehicle_id, out.get("plan") or self.track.race_speed_plan())
        return out

    def stop_mapped_race(self) -> dict[str, Any]:
        vid = self.track.race_vehicle
        self._cancel_race_brake()
        out = self.track.stop_race()
        if vid:
            try:
                self.panic_stop(vid)
            except Exception:
                pass
        return out

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
            # Remember + publish without toggling hold (caller may be reconnect).
            self._remember(
                vehicle_id,
                velocity=cfg["velocity"],
                acceleration=cfg.get("acceleration", 1000),
            )
            self._publish_speed(
                vehicle_id,
                int(cfg["velocity"]),
                int(cfg.get("acceleration", 1000)),
            )
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

    def set_hold(self, vehicle_id: str, enabled: bool, interval_s: float | None = None) -> None:
        interval = float(interval_s if interval_s is not None else _HOLD_INTERVAL_S)
        self._hold_enabled[vehicle_id] = enabled
        if enabled and (self._hold_thread is None or not self._hold_thread.is_alive()):
            self._hold_stop.clear()
            self._hold_thread = threading.Thread(
                target=self._hold_loop,
                args=(interval,),
                daemon=True,
            )
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

        # Match MQTT Explorer: mqtt:// host:1883, optional user/pass, no TLS.
        # Own reconnect scheduler only — paho auto-reconnect + force drop fought each other.
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            clean_session=True,
            reconnect_on_failure=False,
        )
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
                self._connecting = False
                connected.set()
                return
            self._connected = True
            self._connecting = False
            self._reconnect_failures = 0
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
            self._connecting = False
            self._reconnect_failures += 1
            self._emit({"type": "mqttDisconnected", "reason": mqtt_rc_str(reason_code)})
            # Recreate clean client (same approach as re-running test_mqtt.py)
            if self._want_connected:
                self._schedule_reconnect()

        def on_message(c, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                payload = {"raw": msg.payload.decode("utf-8", errors="replace")}
            # MQTT Explorer history looks like a list; live msgs are objects.
            if isinstance(payload, list):
                payload = payload[-1] if payload else {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            # Do not flood UI/debug with every battery tick — route only.
            self._route(msg.topic, payload)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        with self._lock:
            self._client = client

        try:
            # Exact order that worked in test_mqtt.py yesterday
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
            if not connected.wait(timeout=12):
                debug_log.error("mqtt", "CONNACK timeout")
                self._connecting = False
                self._reconnect_failures += 1
                self._emit({"type": "mqttError", "message": "CONNACK timeout (12s)"})
                if self._client is client:
                    with self._lock:
                        to_drop = self._detach_client_unlocked()
                    self._close_client(to_drop)
                if self._want_connected:
                    self._schedule_reconnect()
                return
            if failed_reason:
                msg = f"MQTT rejected: {failed_reason[0]}"
                debug_log.error("mqtt", msg)
                self._connecting = False
                self._reconnect_failures += 1
                self._emit({"type": "mqttError", "message": msg})
                if self._client is client:
                    with self._lock:
                        to_drop = self._detach_client_unlocked()
                    self._close_client(to_drop)
                if self._want_connected:
                    self._schedule_reconnect()
                return
        except Exception as exc:
            debug_log.exception("mqtt", "connect_sync failed", exc)
            self._connecting = False
            self._reconnect_failures += 1
            self._emit({"type": "mqttError", "message": f"connect failed: {exc}"})
            if self._client is client:
                with self._lock:
                    to_drop = self._detach_client_unlocked()
                self._close_client(to_drop)
            if self._want_connected:
                self._schedule_reconnect()

    def _detach_client_unlocked(self) -> Any:
        """Clear client refs under lock; caller must _close_client outside the lock."""
        client = self._client
        self._client = None
        self._connected = False
        self._connecting = False
        return client

    def _close_client(self, client: Any) -> None:
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
        # Chemin lab: Anki/Vehicles/U/{id}/E/wheelDistance
        patterns = [
            f"Anki/Vehicles/U/{vehicle_id}/S/online",
            f"Anki/Vehicles/U/{vehicle_id}/S/status",
            f"Anki/Vehicles/U/{vehicle_id}/S/onTrack",
            f"Anki/Vehicles/U/{vehicle_id}/S/battery/level",
            f"Anki/Vehicles/U/{vehicle_id}/E/wheelDistance",
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

    def _hold_refresh_speed(self, vehicle_id: str) -> None:
        """Race cruise: only re-send speed (no connect/lights/lane — those caused jerk)."""
        cfg = self._desired.get(vehicle_id) or {}
        if "velocity" not in cfg:
            return
        velocity = int(cfg["velocity"])
        if velocity <= 0:
            return
        # Keep accel firm so any tiny coast is corrected immediately at race pace.
        acceleration = int(cfg.get("acceleration", 1200))
        self._publish_speed(vehicle_id, velocity, acceleration)

    def _hold_loop(self, interval_s: float) -> None:
        # Always use race interval even if an older caller passed 1.5s.
        period = min(float(interval_s), _HOLD_INTERVAL_S)
        while not self._hold_stop.wait(period):
            if not self._connected:
                continue
            for vid, on in list(self._hold_enabled.items()):
                if on:
                    try:
                        self._hold_refresh_speed(vid)
                    except Exception as exc:
                        debug_log.warn("mqtt", "hold speed refresh failed", vehicleId=vid, error=str(exc))

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
            # Toujours écouter les traits roues (mapping / latence / course)
            "Anki/Vehicles/U/+/E/wheelDistance",
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

        # Chemin: Anki/Vehicles/U/{vehicleId}/… (MQTT Explorer: Anki → Vehicles → U → id → E → wheelDistance)
        vehicles_branch = parts[1].lower() if len(parts) > 1 else ""
        if (
            len(parts) >= 4
            and parts[0] == "Anki"
            and vehicles_branch in {"vehicles", "vehicules"}
            and parts[2] == "U"
        ):
            vid = parts[3]
            if not vid or vid in {"I", "D"}:
                return
            self._upsert_vehicle(vid)
            leaf = parts[-1]
            parent = parts[-2] if len(parts) >= 2 else ""
            # Exact: …/E/wheelDistance
            is_wheel_distance = leaf == "wheelDistance" and parent == "E"
            val = payload.get("value") if isinstance(payload, dict) else None
            changed = False
            snap: dict[str, Any] | None = None
            wd: tuple[str, float, float, Any] | None = None
            with self._lock:
                v = self._vehicles.setdefault(vid, {"id": vid, "connected": False, "status": "unknown"})
                v["lastSeen"] = time.time()
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
                elif is_wheel_distance:
                    left = right = None
                    candidates = [val]
                    if isinstance(payload, dict):
                        candidates.extend([payload.get("payload"), payload])
                    for cand in candidates:
                        if isinstance(cand, dict) and cand.get("left") is not None and cand.get("right") is not None:
                            left = cand.get("left")
                            right = cand.get("right")
                            break
                    if left is not None and right is not None:
                        v["wheelDistance"] = {"left": left, "right": right}
                        changed = True
                        wd = (
                            vid,
                            float(left),
                            float(right),
                            payload.get("timestamp") if isinstance(payload, dict) else None,
                        )
                        if self.track.mapping or self.track.latency_active or self.track.race_active:
                            debug_log.info(
                                "mqtt",
                                "wheelDistance",
                                topic=f"Anki/Vehicles/U/{vid}/E/wheelDistance",
                                vehicleId=vid,
                                left=left,
                                right=right,
                                mapping=self.track.mapping,
                            )
                snap = dict(v)
            # wheelDistance handled outside lock (may set_speed for race / latency stop)
            if wd is not None:
                vid_u = vid.upper()
                pending_latency = (
                    self.track.latency_active
                    and (self.track.latency_vehicle or "").upper() == vid_u
                )
                was_mapping = (
                    self.track.mapping
                    and (self.track.mapping_vehicle or "").upper() == vid_u
                )
                in_race = (
                    self.track.race_active
                    and (self.track.race_vehicle or "").upper() == vid_u
                )
                self.track.on_wheel_distance(wd[0], wd[1], wd[2], wd[3])
                if pending_latency and self.track.latency_result:
                    try:
                        self.panic_stop(vid)
                    except Exception:
                        pass
                elif was_mapping and not self.track.mapping:
                    try:
                        self.panic_stop(vid)
                    except Exception:
                        pass
                elif in_race and self.track.race_active:
                    try:
                        segs = (self.track.map or {}).get("segments") or []
                        if segs:
                            self._apply_race_plan(vid, self.track.race_speed_plan())
                    except Exception as exc:
                        debug_log.warn("mqtt", "race speed apply failed", error=str(exc))
            if changed and snap is not None and leaf in {
                "status", "connection", "speed", "onTrack", "level", "rssi", "wheelDistance",
            }:
                self._emit({"type": "vehicleUpdate", "vehicleId": vid, "state": snap})

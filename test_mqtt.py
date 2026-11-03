"""
Smoke test — run WHILE connected to Wi-Fi SSID `hyperdrive`:

  python test_mqtt.py

Exit 0 = CONNACK OK (same goal as MQTT Explorer green).
"""

from __future__ import annotations

import socket
import sys
import threading
import time

import paho.mqtt.client as mqtt

HOST = "10.42.0.1"
PORT = 1883
USER = "Your_mom"
# Leave None to match MQTT Explorer (username only, no password field).
PASSWORD = None


def tcp_ok() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=2):
            print(f"TCP {HOST}:{PORT} OK")
            return True
    except OSError as exc:
        print(f"TCP FAIL: {exc}")
        print("Connect to Wi-Fi SSID hyperdrive first.")
        return False


def main() -> int:
    if not tcp_ok():
        return 2

    done = threading.Event()
    result: dict = {}

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"hd-test-{int(time.time()) % 100000}",
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    if USER:
        client.username_pw_set(USER, PASSWORD)

    def on_connect(c, u, flags, rc, props=None):
        # paho 2.x passes ReasonCode — do NOT use int(rc)
        ok = (not rc.is_failure) if hasattr(rc, "is_failure") else str(rc) in ("0", "Success")
        result["rc"] = str(rc)
        result["ok"] = ok
        print("on_connect:", result)
        done.set()

    def on_disconnect(c, u, flags, rc, props=None):
        print("on_disconnect:", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    print(f"Connecting mqtt://{HOST}:{PORT} user={USER!r} password_set={PASSWORD is not None}")
    client.connect(HOST, PORT, 30)
    client.loop_start()
    if not done.wait(12):
        print("CONNACK TIMEOUT")
        client.loop_stop()
        client.disconnect()
        return 1

    ok = bool(result.get("ok"))
    if ok:
        client.subscribe("Anki/#", qos=0)
        print("SUBSCRIBE Anki/# OK — waiting 3s for traffic…")
        time.sleep(3)
        print("SUCCESS")
    else:
        print("REJECTED by broker")

    client.loop_stop()
    client.disconnect()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

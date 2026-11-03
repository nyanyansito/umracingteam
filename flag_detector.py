"""Capture exactly ONE message from a topic and print it, then exit.

Use this instead of scrolling through mqtt_subscriber.py's fast-moving
output when you just need to see one specific message clearly (e.g.
proving a fake bad packet got flagged).

Usage:
    python flag_detector.py umf1/car01/telemetry/fast/processed

If no topic is given, it defaults to the fast-telemetry processed
topic, which is the one the fake-packet demo publishes to.
"""

import json
import sys

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883

DEFAULT_TOPIC = "umf1/car01/telemetry/fast/processed"
topic = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TOPIC


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"Connected. Waiting for an OUT-OF-RANGE message on: {topic}")
        print("(Normal in-range messages will be ignored -- inject your fake packet whenever you're ready)")
        client.subscribe(topic)
    else:
        print(f"Connection failed: {reason_code}")


def on_message(client, userdata, message):
    try:
        data = json.loads(message.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not data.get("any_out_of_range"):
        # Normal, in-range message -- not what we're waiting for.
        # Print a one-line heartbeat so you can see it's alive and
        # actually receiving traffic, then keep waiting.
        print(f"  ...saw a normal (in-range) message, still waiting")
        return

    print("\n" + "=" * 60)
    print(f"CAPTURED an OUT-OF-RANGE message on topic: {message.topic}")
    print("=" * 60)
    print(json.dumps(data, indent=2))
    print("=" * 60)
    print("Done -- stopping.")

    client.disconnect()


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-flag-detector",
)

client.on_connect = on_connect
client.on_message = on_message

print(f"Connecting to {BROKER}:{PORT} ...")

try:
    client.connect(BROKER, PORT)
except (ConnectionRefusedError, OSError) as error:
    print(f"Could not connect to Mosquitto: {error}")
    print("Is Mosquitto actually running? Check with: mosquitto -v")
    sys.exit(1)

try:
    client.loop_forever()
except KeyboardInterrupt:
    print()
    print("Stopped without capturing anything.")
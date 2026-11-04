"""Replay old fast CSV files or lossless multi-topic JSONL recordings."""
import argparse
import base64
import csv
import json
import math
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

from mqtt_runtime import start_mqtt
from telemetry_validation import validate_fast_sample

BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/fast"


def convert_row(row):
    sample = {
        "sequence": int(row["sequence"]),
        "timestamp_ms": int(row["timestamp_ms"]),
        "throttle_percent": float(row["throttle_percent"]),
        "brake_bar": float(row["brake_bar"]),
        "steering_degrees": float(row["steering_degrees"]),
        "speed_kmh": float(row["speed_kmh"]),
    }
    if row.get("session_id"):
        sample["session_id"] = row["session_id"]
    return validate_fast_sample(sample)


def replay_records(file_path, all_topics=False):
    """Yield (time_ms, topic, payload, qos); regenerate derived topics by default."""
    with Path(file_path).open(encoding="utf-8", newline="") as source:
        if Path(file_path).suffix.lower() == ".jsonl":
            for line in source:
                record = json.loads(line)
                topic = record["topic"]
                if not all_topics and (topic.endswith("/processed") or topic.endswith("/telemetry/lap")):
                    continue
                yield (record["received_monotonic_ns"] / 1_000_000, topic,
                       base64.b64decode(record["payload_base64"], validate=True), record["qos"])
        else:
            for row in csv.DictReader(source):
                sample = convert_row(row)
                yield (sample["timestamp_ms"], TOPIC, json.dumps(sample).encode(), 0)


def replay_file(file_path, replay_speed, all_topics=False):
    if not math.isfinite(replay_speed) or replay_speed <= 0:
        raise ValueError("Replay speed must be finite and greater than zero")
    connected = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="umf1-telemetry-replay")

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        connected.clear()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    previous_timestamp = None
    deadline = None
    try:
        start_mqtt(client, BROKER, PORT)
        for timestamp, topic, payload, qos in replay_records(file_path, all_topics):
            while not connected.wait(0.2):
                pass  # Initial broker failure remains retryable and interruptible.
            if deadline is None:
                deadline = time.monotonic()
            if previous_timestamp is not None:
                deadline += max(0, timestamp - previous_timestamp) / 1000 / replay_speed
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            while not connected.wait(0.2):
                pass
            result = client.publish(topic, payload, qos=qos, retain=False)
            result.wait_for_publish(timeout=5)
            if result.rc != mqtt.MQTT_ERR_SUCCESS or not result.is_published():
                raise RuntimeError("Replay publication failed; stopping to avoid silent data loss")
            previous_timestamp = timestamp
    except KeyboardInterrupt:
        print("Replay stopped")
    finally:
        client.disconnect()
        client.loop_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--all-topics", action="store_true",
                        help="Also replay recorded lap/processed topics; stop their live producers first")
    args = parser.parse_args()
    if not args.file.is_file():
        parser.error("Recording not found")
    if not math.isfinite(args.speed) or args.speed <= 0:
        parser.error("Replay speed must be finite and greater than zero")
    replay_file(args.file, args.speed, args.all_topics)


if __name__ == "__main__":
    main()

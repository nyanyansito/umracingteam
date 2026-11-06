"""Record telemetry losslessly; derived lap display traffic is opt-in."""
import base64
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

from mqtt_runtime import run_mqtt
from telemetry_validation import decode_object, json_safe, validate_fast_sample

from telemetry_config import BROKER, PORT
TOPIC = "umf1/car01/#"
FAST_TOPIC = "umf1/car01/telemetry/fast"
FIELD_NAMES = ["sequence", "timestamp_ms", "throttle_percent", "brake_bar",
               "steering_degrees", "speed_kmh", "session_id", "received_time_ns"]


class SessionLogger:
    def __init__(self, folder="logs", include_lap_state=False):
        self.include_lap_state = include_lap_state
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        name = "telemetry_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.csv_path = folder / (name + ".csv")
        self.jsonl_path = folder / (name + ".jsonl")
        self.csv_file = self.csv_path.open("x", newline="", encoding="utf-8")
        try:
            self.jsonl_file = self.jsonl_path.open("x", encoding="utf-8")
        except BaseException:
            self.csv_file.close()
            raise
        self.writer = csv.DictWriter(self.csv_file, fieldnames=FIELD_NAMES)
        self.writer.writeheader()
        self.csv_file.flush()

    def record(self, message):
        # The lap service can regenerate display snapshots from recorded GPS and
        # inputs. Re-recording full reference arrays every two seconds wastes GBs
        # over a long stint; opt in only when diagnosing the state protocol.
        if "/telemetry/lap/" in message.topic and not self.include_lap_state:
            return
        received_time_ns = time.time_ns()
        record = {
            "received_time_ns": received_time_ns,
            "received_monotonic_ns": time.monotonic_ns(),
            "topic": message.topic,
            "qos": message.qos,
            "retain": message.retain,
            "payload_base64": base64.b64encode(message.payload).decode("ascii"),
        }
        try:
            data = decode_object(message.payload)
        except (ValueError, UnicodeDecodeError):
            data = None
        record["data"] = json_safe(data)
        self.jsonl_file.write(json.dumps(record, allow_nan=False) + "\n")
        self.jsonl_file.flush()
        if message.topic == FAST_TOPIC:
            try:
                validate_fast_sample(data)
            except ValueError:
                print("Invalid fast sample preserved in JSONL; omitted from CSV")
                return
            row = {field: data.get(field) for field in FIELD_NAMES}
            row["received_time_ns"] = received_time_ns
            self.writer.writerow(row)
            self.csv_file.flush()

    def close(self):
        self.csv_file.close()
        self.jsonl_file.close()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(TOPIC, qos=1)


def on_message(client, userdata, message):
    userdata.record(message)


def main():
    logger = SessionLogger(include_lap_state=os.environ.get("UMR_LOG_LAP_STATE") == "1")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="umf1-telemetry-logger", userdata=logger)
    client.on_connect = on_connect
    client.on_message = on_message
    print(f"Recording {logger.csv_path} and {logger.jsonl_path}")
    try:
        run_mqtt(client, BROKER, PORT)
    finally:
        logger.close()


if __name__ == "__main__":
    main()

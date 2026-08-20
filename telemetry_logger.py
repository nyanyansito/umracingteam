import csv
import json
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/fast"

LOG_FOLDER = Path("logs")
LOG_FOLDER.mkdir(exist_ok=True)

session_time = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file_path = LOG_FOLDER / f"telemetry_{session_time}.csv"

field_names = [
    "sequence",
    "timestamp_ms",
    "throttle_percent",
    "brake_bar",
    "steering_degrees",
    "speed_kmh",
]

log_file = open(log_file_path, "w", newline="")
csv_writer = csv.DictWriter(log_file, fieldnames=field_names)
csv_writer.writeheader()
log_file.flush()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Logger connected to Mosquitto")
        print(f"Saving telemetry to: {log_file_path}")
        client.subscribe(TOPIC)
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    try:
        telemetry = json.loads(message.payload.decode("utf-8"))

        row = {
            field: telemetry.get(field)
            for field in field_names
        }

        csv_writer.writerow(row)
        log_file.flush()

        print(f"Saved packet {telemetry.get('sequence')}")

    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Logger received an invalid message")


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-telemetry-logger"
)

client.on_connect = on_connect
client.on_message = on_message

try:
    client.connect(BROKER, PORT)
    client.loop_forever()

except KeyboardInterrupt:
    print()
    print("Telemetry logger stopped")

finally:
    log_file.close()
    client.disconnect()
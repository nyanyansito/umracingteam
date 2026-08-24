import argparse
import csv
import json
import time
from pathlib import Path

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/fast"


def convert_row(row):
    return {
        "sequence": int(row["sequence"]),
        "timestamp_ms": int(row["timestamp_ms"]),
        "throttle_percent": float(
            row["throttle_percent"]
        ),
        "brake_bar": float(row["brake_bar"]),
        "steering_degrees": float(
            row["steering_degrees"]
        ),
        "speed_kmh": float(row["speed_kmh"]),
    }


def replay_file(file_path, replay_speed):
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="umf1-telemetry-replay"
    )

    client.connect(BROKER, PORT)
    client.loop_start()

    previous_timestamp_ms = None

    print(f"Replaying: {file_path}")
    print(f"Replay speed: {replay_speed}x")
    print("Press Control + C to stop")

    try:
        with open(file_path, newline="") as csv_file:
            reader = csv.DictReader(csv_file)

            for row in reader:
                telemetry = convert_row(row)
                timestamp_ms = telemetry["timestamp_ms"]

                if previous_timestamp_ms is not None:
                    delay_seconds = (
                        timestamp_ms - previous_timestamp_ms
                    ) / 1000 / replay_speed

                    if delay_seconds > 0:
                        time.sleep(delay_seconds)

                client.publish(
                    TOPIC,
                    json.dumps(telemetry),
                )

                print(
                    f"Replayed packet "
                    f"{telemetry['sequence']}"
                )

                previous_timestamp_ms = timestamp_ms

    except KeyboardInterrupt:
        print()
        print("Replay stopped")

    finally:
        client.loop_stop()
        client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Replay a recorded telemetry CSV file"
    )

    parser.add_argument(
        "file",
        help="Path to the telemetry CSV file",
    )

    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Replay speed multiplier",
    )

    arguments = parser.parse_args()

    file_path = Path(arguments.file)

    if not file_path.exists():
        print(f"File not found: {file_path}")
        return

    if arguments.speed <= 0:
        print("Replay speed must be greater than zero")
        return

    replay_file(file_path, arguments.speed)


if __name__ == "__main__":
    main()

import json
from simulated_track import sample_track
from simulation_clock import SimulationClock
import uuid

import paho.mqtt.client as mqtt
from mqtt_runtime import start_mqtt


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/gps"

UPDATE_RATE_HZ = 10
UPDATE_INTERVAL_SECONDS = 1 / UPDATE_RATE_HZ

def generate_gps(sequence, elapsed_seconds):
    sample = sample_track(elapsed_seconds)
    return {
        "sequence": sequence,
        "timestamp_ms": int(elapsed_seconds * 1000) % (1 << 32),
        "latitude": sample["latitude"],
        "longitude": sample["longitude"],
        "gps_speed_kmh": sample["speed_kmh"],
        "gps_valid": True,
        "track_id": "red_bull_ring",
        "throttle_percent": sample["throttle_percent"],
        "brake_bar": sample["brake_bar"],
    }


def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="umf1-gps-simulator"
    )

    start_mqtt(client, BROKER, PORT)

    print("GPS simulator started")
    print(f"Publishing to {TOPIC}")
    print("Press Control + C to stop")

    clock = SimulationClock(UPDATE_INTERVAL_SECONDS)
    sequence = 0
    session_id = uuid.uuid4().hex

    try:
        while True:
            elapsed = clock.elapsed
            gps_data = generate_gps(sequence, elapsed)
            gps_data["session_id"] = session_id

            client.publish(
                TOPIC,
                json.dumps(gps_data),
            )

            print(
                f"GPS {sequence}: "
                f"lat={gps_data['latitude']}, "
                f"lon={gps_data['longitude']}, "
                f"speed={gps_data['gps_speed_kmh']} km/h"
            )

            sequence = (sequence + 1) % 65536
            clock.advance()

    except KeyboardInterrupt:
        print()
        print("GPS simulator stopped")

    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()

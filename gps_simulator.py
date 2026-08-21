import json
import math
import time

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/gps"

UPDATE_RATE_HZ = 10
UPDATE_INTERVAL_SECONDS = 1 / UPDATE_RATE_HZ

CENTER_LATITUDE = 3.1215
CENTER_LONGITUDE = 101.6532

TRACK_RADIUS_METERS = 100
SIMULATED_LAP_TIME_SECONDS = 45


def generate_gps(sequence, elapsed_seconds):
    lap_position = (
        elapsed_seconds % SIMULATED_LAP_TIME_SECONDS
    ) / SIMULATED_LAP_TIME_SECONDS

    angle = lap_position * 2 * math.pi

    north_meters = TRACK_RADIUS_METERS * math.sin(angle)
    east_meters = TRACK_RADIUS_METERS * math.cos(angle)

    latitude = (
        CENTER_LATITUDE
        + north_meters / 111_320
    )

    longitude = (
        CENTER_LONGITUDE
        + east_meters
        / (
            111_320
            * math.cos(math.radians(CENTER_LATITUDE))
        )
    )

    track_length_meters = 2 * math.pi * TRACK_RADIUS_METERS
    speed_mps = track_length_meters / SIMULATED_LAP_TIME_SECONDS
    speed_kmh = speed_mps * 3.6

    return {
        "sequence": sequence,
        "timestamp_ms": int(elapsed_seconds * 1000),
        "latitude": round(latitude, 7),
        "longitude": round(longitude, 7),
        "gps_speed_kmh": round(speed_kmh, 2),
        "gps_valid": True,
    }


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-gps-simulator"
)

client.connect(BROKER, PORT)
client.loop_start()

print("GPS simulator started")
print(f"Publishing to {TOPIC}")
print("Press Control + C to stop")

start_time = time.monotonic()
sequence = 0

try:
    while True:
        elapsed = time.monotonic() - start_time
        gps_data = generate_gps(sequence, elapsed)

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
        time.sleep(UPDATE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print()
    print("GPS simulator stopped")

finally:
    client.loop_stop()
    client.disconnect()
import json
import math

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883

GPS_TOPIC = "umf1/car01/telemetry/gps"
LAP_TOPIC = "umf1/car01/telemetry/lap"

CENTER_LATITUDE = 3.1215
CENTER_LONGITUDE = 101.6532
TRACK_RADIUS_METERS = 100

MINIMUM_LAP_TIME_SECONDS = 20

meters_per_longitude_degree = (
    111_320
    * math.cos(math.radians(CENTER_LATITUDE))
)

finish_line_inner_longitude = (
    CENTER_LONGITUDE
    + (TRACK_RADIUS_METERS - 30)
    / meters_per_longitude_degree
)

finish_line_outer_longitude = (
    CENTER_LONGITUDE
    + (TRACK_RADIUS_METERS + 30)
    / meters_per_longitude_degree
)

FINISH_LINE_START = (
    finish_line_inner_longitude,
    CENTER_LATITUDE,
)

FINISH_LINE_END = (
    finish_line_outer_longitude,
    CENTER_LATITUDE,
)

previous_position = None
lap_start_timestamp_ms = None
lap_number = 0


def cross_product(point_a, point_b, point_c):
    return (
        (point_b[0] - point_a[0])
        * (point_c[1] - point_a[1])
        - (point_b[1] - point_a[1])
        * (point_c[0] - point_a[0])
    )


def segments_intersect(a, b, c, d):
    cross_1 = cross_product(a, b, c)
    cross_2 = cross_product(a, b, d)
    cross_3 = cross_product(c, d, a)
    cross_4 = cross_product(c, d, b)

    return (
        cross_1 * cross_2 <= 0
        and cross_3 * cross_4 <= 0
    )


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Lap timer connected to Mosquitto")
        print(f"Listening to {GPS_TOPIC}")
        client.subscribe(GPS_TOPIC)
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    global previous_position
    global lap_start_timestamp_ms
    global lap_number

    try:
        gps = json.loads(message.payload.decode("utf-8"))

        if not gps.get("gps_valid", False):
            return

        current_position = (
            gps["longitude"],
            gps["latitude"],
        )

        current_timestamp_ms = gps["timestamp_ms"]

        if lap_start_timestamp_ms is None:
            lap_start_timestamp_ms = current_timestamp_ms
            previous_position = current_position
            print("Lap timer armed")
            return

        if current_timestamp_ms < lap_start_timestamp_ms:
            lap_start_timestamp_ms = current_timestamp_ms
            previous_position = current_position
            lap_number = 0
            print("GPS timestamp reset detected")
            return

        crossed_line = (
            previous_position is not None
            and segments_intersect(
                previous_position,
                current_position,
                FINISH_LINE_START,
                FINISH_LINE_END,
            )
        )

        correct_direction = (
            previous_position is not None
            and previous_position[1] < CENTER_LATITUDE
            and current_position[1] >= CENTER_LATITUDE
        )

        elapsed_seconds = (
            current_timestamp_ms - lap_start_timestamp_ms
        ) / 1000

        if (
            crossed_line
            and correct_direction
            and elapsed_seconds >= MINIMUM_LAP_TIME_SECONDS
        ):
            lap_number += 1

            lap_message = {
                "lap_number": lap_number,
                "lap_time_seconds": round(
                    elapsed_seconds,
                    3,
                ),
                "timestamp_ms": current_timestamp_ms,
            }

            client.publish(
                LAP_TOPIC,
                json.dumps(lap_message),
                qos=1,
            )

            print(
                f"Lap {lap_number}: "
                f"{elapsed_seconds:.3f} seconds"
            )

            lap_start_timestamp_ms = current_timestamp_ms

        previous_position = current_position

    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
    ):
        print("Lap timer received invalid GPS data")


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-lap-timer"
)

client.on_connect = on_connect
client.on_message = on_message

try:
    client.connect(BROKER, PORT)
    client.loop_forever()

except KeyboardInterrupt:
    print()
    print("Lap timer stopped")

finally:
    client.disconnect()
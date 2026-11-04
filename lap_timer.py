"""Directional lap timing that requires continuous, valid GPS fixes."""
import json
import math
import time

import paho.mqtt.client as mqtt

from mqtt_runtime import run_mqtt
from telemetry_validation import decode_object, is_number, is_uint
from simulated_track import FINISH_LINE_START, FINISH_LINE_END

BROKER = "localhost"
PORT = 1883
GPS_TOPIC = "umf1/car01/telemetry/gps"
LAP_TOPIC = "umf1/car01/telemetry/lap"
MINIMUM_LAP_TIME_SECONDS = 20
MAXIMUM_FIX_GAP_SECONDS = 1.0



def cross_product(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a, b, c, d):
    # Bounding boxes are necessary for disjoint collinear segments.
    return (max(min(a[0], b[0]), min(c[0], d[0])) <= min(max(a[0], b[0]), max(c[0], d[0]))
            and max(min(a[1], b[1]), min(c[1], d[1])) <= min(max(a[1], b[1]), max(c[1], d[1]))
            and cross_product(a, b, c) * cross_product(a, b, d) <= 0
            and cross_product(c, d, a) * cross_product(c, d, b) <= 0)


class LapTimer:
    def __init__(self, finish_start=FINISH_LINE_START, finish_end=FINISH_LINE_END,
                 minimum_lap_seconds=MINIMUM_LAP_TIME_SECONDS,
                 maximum_gap_seconds=MAXIMUM_FIX_GAP_SECONDS):
        self.finish_start = finish_start
        self.finish_end = finish_end
        self.minimum_lap_seconds = minimum_lap_seconds
        self.maximum_gap_seconds = maximum_gap_seconds
        self.lap_number = 0
        self.session_id = None
        self.reset_continuity()

    def reset_continuity(self):
        self.crossing = None
        self.previous_position = None
        self.previous_timestamp_ms = None
        self.previous_receive_time = None
        self.elapsed_ms = 0
        self.lap_start_ms = None

    def update(self, gps, now=None):
        self.crossing = None
        now = time.monotonic() if now is None else now
        if not isinstance(gps, dict) or gps.get("gps_valid") is not True:
            self.reset_continuity()
            return None
        latitude, longitude = gps.get("latitude"), gps.get("longitude")
        timestamp = gps.get("timestamp_ms")
        session = gps.get("session_id")
        if (not is_number(latitude) or not -90 <= latitude <= 90
                or not is_number(longitude) or not -180 <= longitude <= 180
                or not is_uint(timestamp, 32)
                or (session is not None and not isinstance(session, str))):
            self.reset_continuity()
            return None
        if session is not None and session != self.session_id:
            self.reset_continuity()
            self.lap_number = 0
            self.session_id = session

        position = (longitude, latitude)
        result = None
        if self.previous_position is not None:
            delta_ms = (timestamp - self.previous_timestamp_ms) % (1 << 32)
            receive_gap = now - self.previous_receive_time
            if delta_ms == 0:
                return None  # Duplicate fix must not create another crossing.
            distance = math.hypot(
                (longitude - self.previous_position[0]) * 111_320 * math.cos(math.radians(latitude)),
                (latitude - self.previous_position[1]) * 111_320,
            )
            if (delta_ms > self.maximum_gap_seconds * 1000
                    or receive_gap < 0
                    or distance > 20 + 100 * delta_ms / 1000):
                self.reset_continuity()
            else:
                previous_elapsed = self.elapsed_ms
                self.elapsed_ms += delta_ms
                before = cross_product(self.finish_start, self.finish_end, self.previous_position)
                after = cross_product(self.finish_start, self.finish_end, position)
                if (before < 0 <= after and segments_intersect(
                        self.previous_position, position, self.finish_start, self.finish_end)):
                    crossing_ms = previous_elapsed + delta_ms * (-before / (after - before))
                    if self.lap_start_ms is None:
                        self.lap_start_ms = crossing_ms
                    elif crossing_ms - self.lap_start_ms >= self.minimum_lap_seconds * 1000:
                        self.lap_number += 1
                        result = {"lap_number": self.lap_number,
                                  "lap_time_seconds": round((crossing_ms - self.lap_start_ms) / 1000, 3),
                                  "timestamp_ms": timestamp}
                        if session is not None:
                            result["session_id"] = session
                        self.lap_start_ms = crossing_ms
                    if self.lap_start_ms == crossing_ms:
                        fraction = -before / (after - before)
                        self.crossing = {
                            "elapsed_ms": crossing_ms,
                            "position": tuple(a + fraction * (b - a) for a, b in zip(self.previous_position, position)),
                        }

        self.previous_position = position
        self.previous_timestamp_ms = timestamp
        self.previous_receive_time = now
        return result


timer = LapTimer()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(GPS_TOPIC)


def on_disconnect(client, userdata, flags, reason_code, properties):
    timer.reset_continuity()


def on_message(client, userdata, message):
    try:
        gps = decode_object(message.payload)
    except (ValueError, UnicodeDecodeError):
        timer.reset_continuity()
        print("Lap timer received invalid GPS data")
        return
    result = timer.update(gps)
    if result is not None:
        client.publish(LAP_TOPIC, json.dumps(result), qos=1)
        print(f"Lap {result['lap_number']}: {result['lap_time_seconds']:.3f} seconds")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="umf1-lap-timer")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    run_mqtt(client, BROKER, PORT)


if __name__ == "__main__":
    main()

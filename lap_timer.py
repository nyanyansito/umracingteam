"""Directional lap timing that requires continuous, valid GPS fixes."""
import json
import math
import time

import paho.mqtt.client as mqtt

from mqtt_runtime import run_mqtt
from telemetry_validation import decode_object, is_number, is_uint
from simulated_track import FINISH_LINE_START, FINISH_LINE_END
from telemetry_config import (BROKER, PORT, FINISH_START, FINISH_END, MINIMUM_LAP_SECONDS,
                              MINIMUM_LAP_DISTANCE, MINIMUM_DEPARTURE, CHECKPOINTS)
from sample_order import SampleOrder

GPS_TOPIC = "umf1/car01/telemetry/gps"
LAP_TOPIC = "umf1/car01/telemetry/lap"
MINIMUM_LAP_TIME_SECONDS = MINIMUM_LAP_SECONDS
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
    def __init__(self, finish_start=None, finish_end=None,
                 minimum_lap_seconds=MINIMUM_LAP_TIME_SECONDS,
                 maximum_gap_seconds=MAXIMUM_FIX_GAP_SECONDS,
                 minimum_distance_m=None, minimum_departure_m=MINIMUM_DEPARTURE, checkpoints=None):
        custom_gate = finish_start is not None or finish_end is not None
        self.finish_start = finish_start or FINISH_START
        self.finish_end = finish_end or FINISH_END
        self.minimum_distance_m = (100 if custom_gate else MINIMUM_LAP_DISTANCE) if minimum_distance_m is None else minimum_distance_m
        self.minimum_departure_m = minimum_departure_m
        self.checkpoints = ([] if custom_gate else CHECKPOINTS) if checkpoints is None else checkpoints
        self.minimum_lap_seconds = minimum_lap_seconds
        self.maximum_gap_seconds = maximum_gap_seconds
        self.lap_number = 0
        self.session_id = None
        self.order = SampleOrder()
        self.source_status = None
        self.invalid_reason = None
        self.reset_continuity()

    def reset_continuity(self):
        self.continuity_version = getattr(self, "continuity_version", 0) + 1
        self.crossing = None
        self.previous_position = None
        self.previous_timestamp_ms = None
        self.previous_receive_time = None
        self.elapsed_ms = 0
        self.lap_start_ms = None
        self.lap_distance = 0.0
        self.departure = 0.0
        self.next_checkpoint = 0

    @staticmethod
    def metres(a, b):
        return ((a[0] - b[0]) * 111320 * math.cos(math.radians(b[1])),
                (a[1] - b[1]) * 111320)

    def checkpoint_passed(self, a, b, gate):
        ax, ay = self.metres(a, gate)
        bx, by = self.metres(b, gate)
        dx, dy = bx - ax, by - ay
        fraction = max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy))) if dx or dy else 0
        return math.hypot(ax + fraction * dx, ay + fraction * dy) <= gate[2]

    def update(self, gps, now=None):
        self.crossing = None
        self.source_status = "invalid"
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
                or ("sequence" in gps and not is_uint(gps["sequence"], 16))
                or (session is not None and not isinstance(session, str))):
            self.reset_continuity()
            return None
        self.source_status = self.order.check(gps)
        if self.source_status in ("late", "duplicate"):
            return None
        if self.source_status == "reset":
            self.reset_continuity()
            self.lap_number = 0
            self.invalid_reason = None
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
                if self.lap_start_ms is not None:
                    self.lap_distance += distance
                    origin = tuple((a + b) / 2 for a, b in zip(self.finish_start, self.finish_end))
                    self.departure = max(self.departure, math.hypot(*self.metres(position, origin)))
                    if self.next_checkpoint < len(self.checkpoints) and self.checkpoint_passed(
                            self.previous_position, position, self.checkpoints[self.next_checkpoint]):
                        self.next_checkpoint += 1
                before = cross_product(self.finish_start, self.finish_end, self.previous_position)
                after = cross_product(self.finish_start, self.finish_end, position)
                if (before < 0 <= after and segments_intersect(
                        self.previous_position, position, self.finish_start, self.finish_end)):
                    crossing_ms = previous_elapsed + delta_ms * (-before / (after - before))
                    if self.lap_start_ms is None:
                        self.lap_start_ms = crossing_ms
                    elif crossing_ms - self.lap_start_ms >= self.minimum_lap_seconds * 1000:
                        eligible = (self.lap_distance >= self.minimum_distance_m
                                    and self.departure >= self.minimum_departure_m
                                    and self.next_checkpoint == len(self.checkpoints))
                        self.invalid_reason = None if eligible else "Incomplete circuit: distance or checkpoints not satisfied"
                        if eligible:
                            self.lap_number += 1
                            result = {"lap_number": self.lap_number,
                                  "lap_time_seconds": round((crossing_ms - self.lap_start_ms) / 1000, 3),
                                  "timestamp_ms": timestamp}
                            if session is not None:
                                result["session_id"] = session
                        self.lap_start_ms = crossing_ms
                    if self.lap_start_ms == crossing_ms:
                        self.lap_distance = 0.0
                        self.departure = 0.0
                        self.next_checkpoint = 0
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
    if getattr(message, "retain", False):
        return
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
    # One service owns lap numbering, comparison and persistence for all viewers.
    from lap_service import main as run_service
    run_service()


if __name__ == "__main__":
    main()

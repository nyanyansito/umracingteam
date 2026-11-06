"""Versioned lap snapshots and append updates shared by service and dashboard."""
import copy
import time
from collections import deque

STATE_TOPIC = "umf1/car01/telemetry/lap/state"
UPDATE_TOPIC = "umf1/car01/telemetry/lap/update"
REQUEST_TOPIC = "umf1/car01/telemetry/lap/request"
REFERENCE_TOPIC = "umf1/car01/telemetry/lap/reference"
APPEND_FIELDS = ("current", "current_inputs", "current_channels", "current_distances", "delta_trace")
STATIC_FIELDS = ("reference", "reference_inputs", "reference_channels", "history", "track_outline", "reference_choices")


def make_update(previous, current):
    result = {k: v for k, v in current.items() if k not in APPEND_FIELDS + STATIC_FIELDS}
    for key in APPEND_FIELDS:
        old, new = previous[key], current[key]
        if len(new) >= len(old) and (not old or new[len(old) - 1] == old[-1]) and (not old or old[0] == new[0]):
            result[key] = {"from": len(old), "items": new[len(old):]}
        else:
            result[key] = new
    return result


class RemoteLapAnalysis:
    """A read-only mirror: a retained snapshot never proves the source is live."""
    def __init__(self):
        from lap_analysis import LapAnalysis
        self.state = LapAnalysis().snapshot()
        self.stream = None
        self.sequence = -1
        self.received = None
        self.live = False
        self.needs_snapshot = True
        self.retired_streams = deque(maxlen=32)

    def interrupt(self):
        self.live = False
        self.needs_snapshot = True

    def receive(self, packet, retained=False, now=None):
        now = time.monotonic() if now is None else now
        if (not isinstance(packet, dict) or packet.get("version") != 1
                or not isinstance(packet.get("stream"), str) or type(packet.get("sequence")) is not int
                or packet["sequence"] < 0 or not isinstance(packet.get("state"), dict)):
            raise ValueError("Invalid lap update")
        sequence, stream = packet["sequence"], packet["stream"]
        if stream in self.retired_streams:
            return False
        if stream == self.stream and sequence <= self.sequence:
            return False
        full = packet.get("full") is True
        if not full and (self.needs_snapshot or stream != self.stream or sequence != self.sequence + 1):
            self.interrupt()
            return False
        candidate = copy.deepcopy(packet["state"] if full else self.state)
        if not full:
            for key, value in packet["state"].items():
                if key in APPEND_FIELDS and isinstance(value, dict):
                    if value.get("from") != len(candidate[key]) or not isinstance(value.get("items"), list):
                        self.interrupt()
                        return False
                    candidate[key].extend(value["items"])
                else:
                    candidate[key] = value
        # Bound allocations and reject malformed snapshots before replacing state.
        from telemetry_validation import is_number
        if not set(self.state).issubset(candidate):
            raise ValueError("Incomplete lap snapshot")
        for key in APPEND_FIELDS + STATIC_FIELDS:
            if not isinstance(candidate[key], list) or len(candidate[key]) > 10001:
                raise ValueError("Invalid lap array")
        for key in ("elapsed", "last", "best", "delta", "gps_age"):
            if candidate[key] is not None and not is_number(candidate[key]):
                raise ValueError("Invalid lap metric")
        for key, width in (("current", None), ("reference", 4)):
            for point in candidate[key]:
                if (not isinstance(point, (list, tuple)) or len(point) not in ((width,) if width else (2, 3))
                        or not all(is_number(v) for v in point)):
                    raise ValueError("Invalid track point")
        for point in candidate["delta_trace"]:
            if not isinstance(point, (list, tuple)) or len(point) != 2 or not is_number(point[0]) or (point[1] is not None and not is_number(point[1])):
                raise ValueError("Invalid delta point")
        if not isinstance(candidate["lap_key"], str) or type(candidate["revision"]) is not int:
            raise ValueError("Invalid lap identity")
        from telemetry_validation import FAST_FIELDS
        for key in ("current_channels", "reference_channels"):
            for sample in candidate[key]:
                if not isinstance(sample, dict) or any(sample.get(k) is not None and not is_number(sample[k]) for k in FAST_FIELDS):
                    raise ValueError("Invalid driver input")
        if self.stream is not None and self.stream != stream:
            self.retired_streams.append(self.stream)
        self.state = candidate
        self.stream, self.sequence = stream, sequence
        self.received, self.live = now, not retained
        self.needs_snapshot = False
        return True

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        state = copy.deepcopy(self.state)
        age = None if self.received is None or state["gps_age"] is None else state["gps_age"] + max(0, now - self.received)
        state["fresh"] = bool(state["fresh"] and self.live and not self.needs_snapshot and age is not None and age <= 1)
        state["gps_age"] = age
        if not state["fresh"]:
            state["delta"] = None
        return state

    def clock_state(self, now=None):
        now = time.monotonic() if now is None else now
        age = None if self.received is None or self.state["gps_age"] is None else self.state["gps_age"] + max(0, now - self.received)
        return {"elapsed": self.state["elapsed"], "age": age,
                "running": self.live and not self.needs_snapshot and self.state["fresh"] and age is not None and age <= 1,
                "lap_key": self.state["lap_key"], "max_extrapolation": .15}

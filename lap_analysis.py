"""Live GPS lap state and a spatial delta against the session's best lap."""
import math
import bisect
import time
from collections import deque

from lap_timer import LapTimer, FINISH_LINE_START, FINISH_LINE_END
from telemetry_validation import is_number
from simulated_track import COORDINATES


class LapAnalysis:
    def __init__(self, timer=None):
        self.timer = timer or LapTimer()
        self.origin = tuple((a + b) / 2 for a, b in zip(self.timer.finish_start, self.timer.finish_end))
        self.reference = []
        self.reference_distances = []
        self.reference_time = None
        self.reference_lap = None
        self.current = []
        self.history = deque(maxlen=50)
        self.approach = deque(maxlen=1000)
        self.delta_trace = deque(maxlen=10000)
        self.progress = 0.0
        self.delta = None
        self.last_receive = None
        self.session_id = None
        self.lap_top_speed = None
        self.session_top_speed = None
        self.completed_count = 0
        self.best_top_speed = None
        self.track_id = None
        self.current_inputs = []
        self.reference_inputs = []
        self.approach_inputs = deque(maxlen=1000)

    def xy(self, position):
        lon, lat = position
        return ((lon - self.origin[0]) * 111320 * math.cos(math.radians(self.origin[1])),
                (lat - self.origin[1]) * 111320)

    def interrupt(self):
        self.timer.reset_continuity()
        self.current = []
        self.current_inputs = []
        self.approach.clear()
        self.approach_inputs.clear()
        self.delta_trace.clear()
        self.delta = None
        self.progress = 0.0
        self.last_receive = None
        self.lap_top_speed = None

    def update(self, gps, now=None):
        now = time.monotonic() if now is None else now
        if isinstance(gps, dict):
            session = gps.get("session_id")
            timestamp = gps.get("timestamp_ms")
            previous = self.timer.previous_timestamp_ms
            reboot = (type(timestamp) is int and previous is not None
                      and (timestamp - previous) % (1 << 32) > (1 << 31))
            if (isinstance(session, str) and session != self.session_id) or reboot:
                self.interrupt()
                self.reference = []
                self.reference_inputs = []
                self.reference_distances = []
                self.reference_time = self.reference_lap = None
                self.history.clear()
                self.timer.lap_number = 0
                self.session_top_speed = None
                self.best_top_speed = None
                self.completed_count = 0
                self.session_id = session
        lap = self.timer.update(gps, now=now)
        if self.timer.previous_position is None:
            self.interrupt()
            return
        point = self.xy(self.timer.previous_position)
        mode = input_mode(gps)
        self.track_id = gps.get("track_id")
        speed = gps.get("gps_speed_kmh")
        speed = speed if is_number(speed) and speed >= 0 else None
        if speed is not None:
            self.session_top_speed = max(self.session_top_speed or 0, speed)
        self.last_receive = self.timer.previous_receive_time
        if self.timer.lap_start_ms is None:
            self.current = []
            self.current_inputs = []
            self.delta_trace.clear()
            self.delta = None
            self.progress = 0.0
            self.approach.append(point)
            self.approach_inputs.append(mode)
            return
        crossing = self.timer.crossing
        if crossing:
            boundary = self.xy(crossing["position"])
            if lap and self.current:
                duration = (crossing["elapsed_ms"] - self.current_start_ms) / 1000
                finished = self.current + [(boundary[0], boundary[1], duration)]
                old_best = self.reference_time
                self.completed_count += 1
                self.history.append({**lap, "lap_time_seconds": duration,
                                     "top_speed_kmh": self.lap_top_speed,
                                     "delta": None if old_best is None else duration - old_best})
                if old_best is None or duration < old_best:
                    distance = 0.0
                    self.reference = []
                    for x, y, seconds in finished:
                        if self.reference:
                            distance += math.hypot(x - self.reference[-1][0], y - self.reference[-1][1])
                        self.reference.append((x, y, seconds, distance))
                    self.reference_time = duration
                    self.reference_inputs = self.current_inputs + [mode]
                    self.reference_distances = [p[3] for p in self.reference]
                    self.reference_lap = lap["lap_number"]
                    self.best_top_speed = self.lap_top_speed
            self.current_start_ms = crossing["elapsed_ms"]
            self.current = [(boundary[0], boundary[1], 0.0)]
            self.current_inputs = [mode]
            self.approach.clear()
            self.approach_inputs.clear()
            self.progress = 0.0
            self.delta_trace.clear()
            self.delta = None
            self.lap_top_speed = None
        if speed is not None:
            self.lap_top_speed = max(self.lap_top_speed or 0, speed)
        elapsed = (self.timer.elapsed_ms - self.timer.lap_start_ms) / 1000
        if self.current and elapsed <= self.current[-1][2]:
            return  # Duplicate source timestamps do not extend a lap trace.
        self.current.append((*point, elapsed))
        self.current_inputs.append(mode)
        if len(self.current) > 10000 or elapsed > 600:
            self.interrupt()
            return
        self.delta = self.compare(point, elapsed)
        if self.delta is not None:
            self.delta_trace.append((self.progress, self.delta))

    def compare(self, point, elapsed):
        if len(self.reference) < 2:
            return None
        candidates = []
        start = max(0, bisect.bisect_left(self.reference_distances, self.progress - 3) - 1)
        end = min(len(self.reference) - 1, bisect.bisect_right(self.reference_distances, self.progress + 30))
        for index in range(start, end):
            a, b = self.reference[index], self.reference[index + 1]
            # Stay near forward progress to avoid jumping to another part
            # of a circuit at crossings or adjacent straights.
            if b[3] < self.progress - 3 or a[3] > self.progress + 30:
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            length2 = dx * dx + dy * dy
            if length2 == 0:
                continue
            fraction = max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2))
            distance = math.hypot(point[0] - a[0] - fraction * dx, point[1] - a[1] - fraction * dy)
            progress = a[3] + fraction * (b[3] - a[3])
            if progress >= self.progress - 3:
                candidates.append((distance, progress, a[2] + fraction * (b[2] - a[2])))
        if not candidates:
            return None
        distance, progress, reference_seconds = min(candidates)
        if distance > 20:
            return None
        self.progress = max(self.progress, progress)
        return elapsed - reference_seconds

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        fresh = self.last_receive is not None and 0 <= now - self.last_receive <= self.timer.maximum_gap_seconds
        return {
            "fresh": fresh,
            "lap_number": self.timer.lap_number + 1 if self.current else None,
            "elapsed": self.current[-1][2] if self.current else None,
            "last": self.history[-1]["lap_time_seconds"] if self.history else None,
            "best": self.reference_time, "reference_lap": self.reference_lap,
            "delta": self.delta if fresh else None,
            "current": list(self.current) if self.current else list(self.approach),
            "reference": list(self.reference), "history": list(self.history),
            "current_inputs": list(self.current_inputs) if self.current else list(self.approach_inputs),
            "reference_inputs": list(self.reference_inputs),
            "delta_trace": list(self.delta_trace),
            "lap_top_speed": self.lap_top_speed,
            "session_top_speed": self.session_top_speed,
            "best_top_speed": self.best_top_speed,
            "completed_count": self.completed_count,
            "track_outline": [self.xy(p) for p in COORDINATES] if self.track_id == "red_bull_ring" else [],
        }

    def clock_state(self, now=None):
        """Small snapshot for the browser clock, without copying chart arrays."""
        now = time.monotonic() if now is None else now
        age = None if self.last_receive is None else max(0, now - self.last_receive)
        return {
            "elapsed": self.current[-1][2] if self.current else None,
            "age": age,
            "running": bool(self.current) and age is not None and age <= self.timer.maximum_gap_seconds,
            "lap_key": f"{self.session_id}:{getattr(self, 'current_start_ms', None)}:{self.timer.lap_number}",
            "max_extrapolation": .15,
        }


def input_mode(sample):
    throttle, brake = sample.get("throttle_percent"), sample.get("brake_bar")
    if not is_number(throttle) or not 0 <= throttle <= 100 or not is_number(brake) or not 0 <= brake <= 100:
        return "unknown"
    return "brake" if brake > 1 else "throttle" if throttle > 5 else "coast"

"""Live GPS lap state and a spatial delta against the session's best lap."""
import math
import time
from collections import deque

from lap_timer import LapTimer, FINISH_LINE_START, FINISH_LINE_END


class LapAnalysis:
    def __init__(self, timer=None):
        self.timer = timer or LapTimer()
        self.origin = tuple((a + b) / 2 for a, b in zip(FINISH_LINE_START, FINISH_LINE_END))
        self.reference = []
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

    def xy(self, position):
        lon, lat = position
        return ((lon - self.origin[0]) * 111320 * math.cos(math.radians(self.origin[1])),
                (lat - self.origin[1]) * 111320)

    def interrupt(self):
        self.timer.reset_continuity()
        self.current = []
        self.approach.clear()
        self.delta_trace.clear()
        self.delta = None
        self.progress = 0.0
        self.last_receive = None

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
                self.reference_time = self.reference_lap = None
                self.history.clear()
                self.timer.lap_number = 0
                self.session_id = session
        lap = self.timer.update(gps, now=now)
        if self.timer.previous_position is None:
            self.interrupt()
            return
        point = self.xy(self.timer.previous_position)
        self.last_receive = self.timer.previous_receive_time
        if self.timer.lap_start_ms is None:
            self.current = []
            self.delta_trace.clear()
            self.delta = None
            self.progress = 0.0
            self.approach.append(point)
            return
        crossing = self.timer.crossing
        if crossing:
            boundary = self.xy(crossing["position"])
            if lap and self.current:
                duration = (crossing["elapsed_ms"] - self.current_start_ms) / 1000
                finished = self.current + [(boundary[0], boundary[1], duration)]
                old_best = self.reference_time
                self.history.append({**lap, "lap_time_seconds": duration,
                                     "delta": None if old_best is None else duration - old_best})
                if old_best is None or duration < old_best:
                    distance = 0.0
                    self.reference = []
                    for x, y, seconds in finished:
                        if self.reference:
                            distance += math.hypot(x - self.reference[-1][0], y - self.reference[-1][1])
                        self.reference.append((x, y, seconds, distance))
                    self.reference_time = duration
                    self.reference_lap = lap["lap_number"]
            self.current_start_ms = crossing["elapsed_ms"]
            self.current = [(boundary[0], boundary[1], 0.0)]
            self.approach.clear()
            self.progress = 0.0
            self.delta_trace.clear()
            self.delta = None
        elapsed = (self.timer.elapsed_ms - self.timer.lap_start_ms) / 1000
        if self.current and elapsed <= self.current[-1][2]:
            return  # Duplicate source timestamps do not extend a lap trace.
        self.current.append((*point, elapsed))
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
        for a, b in zip(self.reference, self.reference[1:]):
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
        if not fresh and self.last_receive is not None:
            self.interrupt()
        return {
            "fresh": fresh,
            "lap_number": self.timer.lap_number + 1 if self.current else None,
            "elapsed": self.current[-1][2] if fresh and self.current else None,
            "last": self.history[-1]["lap_time_seconds"] if self.history else None,
            "best": self.reference_time, "reference_lap": self.reference_lap,
            "delta": self.delta if fresh else None,
            "current": list(self.current) if self.current else list(self.approach),
            "reference": list(self.reference), "history": list(self.history),
            "delta_trace": list(self.delta_trace),
        }

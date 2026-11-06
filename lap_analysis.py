"""Live GPS lap state and a spatial delta against the session's best lap."""
import math
import bisect
import time
from collections import deque

from lap_timer import LapTimer, FINISH_LINE_START, FINISH_LINE_END
from telemetry_validation import FAST_FIELDS, is_number
from simulated_track import COORDINATES
from telemetry_config import TRACK_ID


class LapAnalysis:
    def __init__(self, timer=None):
        self.timer = timer or LapTimer()
        self.continuity_version = self.timer.continuity_version
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
        self.current_channels = []
        self.current_distances = []
        self.reference_channels = []
        self.laps = {}
        self.selected_reference = None
        self.best_time = None
        self.best_lap = None
        self.last_match_elapsed = 0.0
        self.match_lost = False
        self.revision = 0
        self.source_offset = None

    def xy(self, position):
        lon, lat = position
        return ((lon - self.origin[0]) * 111320 * math.cos(math.radians(self.origin[1])),
                (lat - self.origin[1]) * 111320)

    def interrupt(self):
        self.timer.reset_continuity()
        self.continuity_version = self.timer.continuity_version
        self._clear_current()

    def _clear_current(self):
        self.current = []
        self.current_inputs = []
        self.current_channels = []
        self.current_distances = []
        self.approach.clear()
        self.approach_inputs.clear()
        self.delta_trace.clear()
        self.delta = None
        self.progress = 0.0
        self.last_receive = None
        self.lap_top_speed = None
        self.last_match_elapsed = 0.0
        self.match_lost = False

    def update(self, gps, now=None):
        now = time.monotonic() if now is None else now
        lap = self.timer.update(gps, now=now)
        if self.timer.source_status in ("duplicate", "late"):
            return
        if self.continuity_version != self.timer.continuity_version:
            self._clear_current()
            self.continuity_version = self.timer.continuity_version
        if self.timer.source_status == "reset":
            self.reference = []
            self.reference_inputs = []
            self.reference_channels = []
            self.reference_distances = []
            self.reference_time = self.reference_lap = None
            self.history.clear()
            self.laps.clear()
            self.best_time = self.best_lap = self.best_top_speed = None
            self.selected_reference = None
            self.session_top_speed = None
            self.completed_count = 0
            self.current = []
            self.current_channels = []
            self.current_distances = []
            self.revision += 1
            self.source_offset = None
        if self.timer.source_status != "invalid":
            self.session_id = self.timer.session_id
        if self.timer.previous_position is None:
            self.interrupt()
            return
        point = self.xy(self.timer.previous_position)
        mode = input_mode(gps)
        channels = {field: gps.get(field) if is_number(gps.get(field)) else None for field in FAST_FIELDS}
        if channels["speed_kmh"] is None and is_number(gps.get("gps_speed_kmh")):
            channels["speed_kmh"] = gps["gps_speed_kmh"]
        self.track_id = gps.get("track_id")
        speed = gps.get("gps_speed_kmh")
        speed = speed if is_number(speed) and speed >= 0 else None
        if speed is not None:
            self.session_top_speed = max(self.session_top_speed or 0, speed)
        self.last_receive = self.timer.previous_receive_time
        offset = now - self.timer.order.unwrapped_ms / 1000
        self.source_offset = offset if self.source_offset is None else min(self.source_offset, offset)
        if self.timer.lap_start_ms is None:
            self.current = []
            self.current_inputs = []
            self.current_channels = []
            self.current_distances = []
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
                old_best = self.best_time
                self.completed_count += 1
                self.history.append({**lap, "lap_time_seconds": duration,
                                     "top_speed_kmh": self.lap_top_speed,
                                     "delta": None if old_best is None else duration - old_best})
                distance, points = 0.0, []
                for x, y, seconds in finished:
                    if points:
                        distance += math.hypot(x - points[-1][0], y - points[-1][1])
                    points.append((x, y, seconds, distance))
                number = lap["lap_number"]
                self.laps[number] = {"points": points, "inputs": self.current_inputs + [mode],
                                     "channels": self.current_channels + [channels], "seconds": duration}
                if old_best is None or duration < old_best:
                    self.best_time, self.best_lap = duration, number
                    self.best_top_speed = self.lap_top_speed
                self._set_reference(self.selected_reference or self.best_lap)
                keep = {row["lap_number"] for row in self.history} | {self.best_lap, self.reference_lap}
                self.laps = {n: data for n, data in self.laps.items() if n in keep}
                self.revision += 1
            self.current_start_ms = crossing["elapsed_ms"]
            self.current = [(boundary[0], boundary[1], 0.0)]
            self.current_inputs = [mode]
            self.current_channels = [channels]
            self.current_distances = [0.0]
            self.approach.clear()
            self.approach_inputs.clear()
            self.progress = 0.0
            self.delta_trace.clear()
            self.delta = None
            self.lap_top_speed = None
            self.last_match_elapsed = 0.0
            self.match_lost = False
        if speed is not None:
            self.lap_top_speed = max(self.lap_top_speed or 0, speed)
        elapsed = (self.timer.elapsed_ms - self.timer.lap_start_ms) / 1000
        if self.current and elapsed <= self.current[-1][2]:
            return  # Duplicate source timestamps do not extend a lap trace.
        self.current.append((*point, elapsed))
        self.current_inputs.append(mode)
        self.current_channels.append(channels)
        if len(self.current) > 10000 or elapsed > 600:
            self.interrupt()
            return
        self.delta = self.compare(point, elapsed)
        if self.reference:
            self.current_distances.append(self.progress if self.delta is not None else None)
        else:
            distance = (self.current_distances[-1] or 0) if self.current_distances else 0
            if len(self.current) > 1:
                distance += math.dist(self.current[-1][:2], self.current[-2][:2])
            self.current_distances.append(distance)
        if self.delta is not None:
            self.delta_trace.append((self.progress, self.delta))
        elif self.delta_trace and self.delta_trace[-1][1] is not None:
            self.delta_trace.append((self.progress, None))

    def _set_reference(self, number):
        if number not in self.laps:
            return
        lap = self.laps[number]
        self.reference = lap["points"]
        self.reference_inputs = lap["inputs"]
        self.reference_channels = lap["channels"]
        self.reference_time, self.reference_lap = lap["seconds"], number
        self.reference_distances = [p[3] for p in self.reference]

    def select_reference(self, number=None):
        if number is not None and (type(number) is not int or number not in self.laps):
            raise ValueError("Reference lap is not stored in this session")
        self.selected_reference = number
        self._set_reference(number or self.best_lap)
        self.progress = self.last_match_elapsed = 0.0
        self.delta = None
        self.delta_trace.clear()
        self.current_distances = []
        # Rebuild the current comparison against the newly selected lap.
        for x, y, seconds in self.current:
            self.delta = self.compare((x, y), seconds)
            self.delta_trace.append((self.progress, self.delta))
            self.current_distances.append(self.progress if self.delta is not None else None)
        self.revision += 1

    def compare(self, point, elapsed):
        if len(self.reference) < 2:
            return None
        candidates = []
        # Expand only as far as physically reachable since the last match.
        # A fixed 30 m window can never reacquire a car that has driven beyond it.
        horizon = max(30, max(0, elapsed - self.last_match_elapsed) * 60)
        start = max(0, bisect.bisect_left(self.reference_distances, self.progress - 3) - 1)
        end = min(len(self.reference) - 1, bisect.bisect_right(self.reference_distances, self.progress + horizon))
        for index in range(start, end):
            a, b = self.reference[index], self.reference[index + 1]
            # Stay near forward progress to avoid jumping to another part
            # of a circuit at crossings or adjacent straights.
            if b[3] < self.progress - 3 or a[3] > self.progress + horizon:
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
            self.match_lost = True
            return None
        distance, progress, reference_seconds = min(candidates)
        if distance > 20:
            self.match_lost = True
            return None
        if self.match_lost and any(d <= distance + 2 and abs(p - progress) > 40 for d, p, _ in candidates):
            return None  # Adjacent straights/crossovers remain ambiguous.
        self.progress = max(self.progress, progress)
        self.last_match_elapsed = elapsed
        self.match_lost = False
        return elapsed - reference_seconds

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        age = self.sample_age(now)
        fresh = age is not None and age <= self.timer.maximum_gap_seconds
        return {
            "fresh": fresh,
            "lap_number": self.timer.lap_number + 1 if self.current else None,
            "elapsed": self.current[-1][2] if self.current else None,
            "last": self.history[-1]["lap_time_seconds"] if self.history else None,
            "best": self.best_time, "best_lap": self.best_lap, "reference_lap": self.reference_lap,
            "reference_time": self.reference_time,
            "delta": self.delta if fresh else None,
            "current": list(self.current) if self.current else list(self.approach),
            "reference": list(self.reference), "history": list(self.history),
            "current_inputs": list(self.current_inputs) if self.current else list(self.approach_inputs),
            "reference_inputs": list(self.reference_inputs),
            "current_channels": list(self.current_channels),
            "current_distances": list(self.current_distances),
            "reference_channels": list(self.reference_channels),
            "delta_trace": list(self.delta_trace),
            "lap_top_speed": self.lap_top_speed,
            "session_top_speed": self.session_top_speed,
            "best_top_speed": self.best_top_speed,
            "completed_count": self.completed_count,
            "track_outline": [self.xy(p) for p in COORDINATES] if self.track_id == TRACK_ID == "red_bull_ring" else [],
            "reference_choices": sorted(self.laps),
            "selected_reference": self.selected_reference,
            "invalid_reason": self.timer.invalid_reason,
            "session_id": self.session_id,
            "lap_key": self.clock_state(now)["lap_key"],
            "revision": self.revision,
            "gps_age": age,
            "gps_missed": self.timer.order.missed,
            "gps_rejected": self.timer.order.rejected,
        }

    def sample_age(self, now):
        if self.last_receive is None:
            return None
        delay = 0 if self.source_offset is None else now - self.timer.order.unwrapped_ms / 1000 - self.source_offset
        return max(0, now - self.last_receive, delay)

    def clock_state(self, now=None):
        """Small snapshot for the browser clock, without copying chart arrays."""
        now = time.monotonic() if now is None else now
        age = self.sample_age(now)
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

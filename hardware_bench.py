"""Read UMR bench frames over STM32 USB or the ESP32 TCP bridge."""

from collections import deque
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime
from math import isfinite
import re
import threading
import time

import serial

from pedal_sync import MOD, PedalClock, elapsed
from lap_analysis import LapAnalysis
from lap_timer import LapTimer
from session_store import write_json
from telemetry_config import TRACK_ID
from telemetry_validation import decode_object, is_number

BAUD = 115200
VOLTS_PER_COUNT = 4.096 / 32768  # ADS1115 gain setting in the bench sketch.
THROTTLE_ADC_RAIL_MV = 32767 * VOLTS_PER_COUNT * 1000
SENSOR_TIMEOUT = 2.0
GPS_TIMEOUT = 3.0
THROTTLE_FRAME = re.compile(rb"UMR_THROTTLE,(-?[0-9]{1,5})?")
BRAKE_FRAME = re.compile(rb"BRAKE_V=(-?[0-9]\.[0-9]{3})")
NMEA_FRAME = re.compile(rb"\$([A-Z]{2}[A-Z0-9]{3},[\x20-\x29\x2b-\x7e]+)\*([0-9A-Fa-f]{2})")


def checked_nmea(frame):
    match = NMEA_FRAME.fullmatch(frame)
    if match is None:
        return None
    body, expected = match.groups()
    checksum = 0
    for byte in body:
        checksum ^= byte
    if checksum == int(expected, 16):
        return body.decode("ascii").split(",")
    return None


def coordinate(value, hemisphere, degree_digits, positive, negative, limit):
    if hemisphere not in (positive, negative):
        raise ValueError("Invalid hemisphere")
    if re.fullmatch(rf"[0-9]{{{degree_digits + 2}}}(?:\.[0-9]+)?", value) is None:
        raise ValueError("Invalid coordinate format")
    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    decimal = degrees + minutes / 60
    if minutes >= 60 or decimal > limit:
        raise ValueError("Coordinate out of range")
    return -decimal if hemisphere == negative else decimal


def rmc_position(fields):
    # RMC's fix status is field 2. Its optional navigation-status field is separate.
    if len(fields) < 7 or fields[2] != "A":
        return None
    if len(fields) > 12 and fields[12] not in ("", "A", "D", "F", "R"):
        return None  # Accept GNSS fixes and older sentences without a mode.
    try:
        latitude = coordinate(fields[3], fields[4], 2, "N", "S", 90)
        longitude = coordinate(fields[5], fields[6], 3, "E", "W", 180)
    except ValueError:
        return None
    return latitude, longitude


def percent_of_span(value, released, full_scale):
    fraction = (value - released) / (full_scale - released)
    return 100 * max(0, min(1, fraction))


@dataclass(frozen=True)
class BrakeCalibration:
    released_v: float = 0.490
    full_scale_v: float = 4.500

    def __post_init__(self):
        if not 0 <= self.released_v < self.full_scale_v <= 5:
            raise ValueError("Use a released voltage below the full-scale voltage, both within 0–5 V.")

    def percent(self, volts):
        if volts is None or not 0 <= volts <= 5:
            return None
        return percent_of_span(volts, self.released_v, self.full_scale_v)


@dataclass(frozen=True)
class ThrottleCalibration:
    released_mv: float
    pressed_mv: float
    bench_only: InitVar[bool] = False

    def __post_init__(self, bench_only):
        if self.released_mv == self.pressed_mv:
            raise ValueError("Use two distinct throttle readings for released and fully pressed.")
        if bench_only and not self.uses_fixture_range:
            raise ValueError("Throttle bench calibration requires both readings between 5 and 250 mV.")
        if not (self.in_adc_range(self.released_mv) and self.in_adc_range(self.pressed_mv)):
            raise ValueError(f"Throttle readings must be above 5 mV and below {THROTTLE_ADC_RAIL_MV:g} mV.")

    @staticmethod
    def in_fixture_range(millivolts):
        # The specified divider reads about 300 mV open and 0 mV shorted.
        return is_number(millivolts) and 5 < millivolts < 250

    @staticmethod
    def in_adc_range(millivolts):
        return is_number(millivolts) and 5 < millivolts < THROTTLE_ADC_RAIL_MV

    @property
    def uses_fixture_range(self):
        return self.in_fixture_range(self.released_mv) and self.in_fixture_range(self.pressed_mv)

    def percent(self, millivolts):
        # Preserve the open-circuit check for calibrations in the original divider's range.
        valid = self.in_fixture_range if self.uses_fixture_range else self.in_adc_range
        if not valid(millivolts):
            return None
        return percent_of_span(millivolts, self.released_mv, self.pressed_mv)

    @classmethod
    def load(cls, path, *, bench_only=False):
        try:
            saved = decode_object(path.read_bytes())
        except FileNotFoundError:
            return None
        return cls(saved.get("released_mv"), saved.get("pressed_mv"), bench_only=bench_only)

    def save(self, path):
        write_json(path, {"released_mv": self.released_mv, "pressed_mv": self.pressed_mv})


def rmc_fix(fields):
    position = rmc_position(fields)
    if position is None or len(fields) <= 9:
        return None
    try:
        clock, _, fraction = fields[1].partition(".")
        if len(clock) != 6 or len(fields[9]) != 6:
            return None
        utc = datetime.strptime(fields[9] + clock, "%d%m%y%H%M%S").replace(tzinfo=UTC)
        millis = int((fraction + "000")[:3])
        timestamp = (int(utc.timestamp()) * 1000 + millis) % (1 << 32)
    except ValueError:
        return None
    fix = dict(gps_valid=True, timestamp_ms=timestamp, latitude=position[0], longitude=position[1])
    try:
        speed = float(fields[7]) * 1.852
        if isfinite(speed) and speed >= 0:
            fix["gps_speed_kmh"] = speed
    except ValueError:
        pass  # Position can still be usable when speed was omitted.
    return fix


class SerialBench:
    def __init__(self, port, calibration=None, *, throttle_only=False,
                 track_configured=TRACK_ID != "red_bull_ring", brake_calibration=None,
                 brake_disconnected=False, record=None):
        self.last_map_point = None
        self.last_valid_arrival = None
        self.record = record
        self.port = port
        self.calibration = calibration
        self.brake_calibration = brake_calibration or BrakeCalibration()
        self.brake_disconnected = brake_disconnected
        self.throttle_only = throttle_only
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.origin_time = time.monotonic()
        self.lap = LapAnalysis(LapTimer(maximum_gap_seconds=GPS_TIMEOUT, timing_enabled=track_configured))
        self.history = deque(maxlen=1000)
        self.brake_history = deque(maxlen=1000)
        self.generation = 0
        self.brake_seen = False
        self.connection_changed(f"Opening {port}…")

    def connection_changed(self, status):
        with self.lock:
            self.status = status
            self.counts = None
            self.sensor_time = None
            self.brake_voltage = None
            self.brake_time = None
            self.brake_status = "Waiting for brake data"
            self.nmea_time = None
            self.rmc_time = None
            self.position = None
            self.pedal_clock = PedalClock()
            self.map_points = deque(maxlen=1000)
            self.map_break = False
            self.fix_times = deque(maxlen=16)
            self.sync_status = "Waiting for timestamped GPS"
            self.match_offset = None
            self.history.clear()
            self.brake_history.clear()
            self.generation += 1
            self.revision = 0
            self.lap.interrupt()

    def receive(self, line):
        if not line.endswith(b"\n"):
            return  # Incomplete frame, or a line longer than the read limit.
        frame = line.rstrip(b"\r\n")
        now = time.monotonic()
        if self.record is not None:
            self.record.write(line)
        if frame == b"UMR_BOOT":
            self.connection_changed(self.status)
            return
        if frame.startswith(b"UMR_ADC,"):
            self.receive_adc(frame, now)
            return
        stamp = None
        if frame.startswith(b"UMR_RMC,"):
            parts = frame.split(b",", 3)
            if len(parts) != 4 or not parts[1].isdigit() or (parts[2] and not parts[2].isdigit()):
                return
            rx = int(parts[1])
            pulse = int(parts[2]) if parts[2] else None
            if rx >= MOD or (pulse is not None and pulse >= MOD):
                return
            stamp = rx, pulse
            frame = parts[3]
        if self.throttle_only or frame.startswith(b"UMR_THROTTLE,"):
            self.receive_throttle(frame, now)
            return

        brake = BRAKE_FRAME.fullmatch(frame)
        if brake is not None or frame == b"BRAKE_I2C_ERROR":
            if self.brake_disconnected:
                return
            volts = None if brake is None else float(brake[1])
            with self.lock:
                if self.brake_time is not None and now - self.brake_time > SENSOR_TIMEOUT:
                    self.brake_history.append((None, None))
                self.brake_seen = True
                self.brake_voltage = volts
                self.brake_time = now
                if volts is None:
                    self.brake_status = "ADS1115 communication error"
                elif 0 <= volts <= 5:
                    self.brake_status = ""
                else:
                    self.brake_status = "Brake voltage outside the 0–5 V input range"
                self.brake_history.append((now - self.origin_time, self.brake_calibration.percent(volts)))
                self.revision += 1
            return

        fields = checked_nmea(frame)
        if fields is None:
            return
        with self.lock:
            self.nmea_time = now
            if fields[0][2:] != "RMC":
                return
            fix = rmc_fix(fields)
            if fix is None:
                self.lap.interrupt()
                self.rmc_time = now
                self.position = None
                self.map_break = True
                self.fix_times.clear()
                return
            # The GPS source clock drives lap timing. A host-side outage must
            # still break the trace, even if a receiver replays a recent fix.
            if self.rmc_time is not None and now - self.rmc_time > GPS_TIMEOUT:
                self.lap.interrupt()
            separated = self.map_break or (self.rmc_time is not None and now - self.rmc_time > GPS_TIMEOUT)
            if stamp is None:
                values, status, offset = {}, "GPS only · no source timestamps", None
            else:
                fraction = int((fields[1].partition(".")[2] + "000")[:3])
                values, status, offset = self.pedal_clock.match(*stamp, fraction)
                fix.update(values)
            continuity = self.lap.timer.continuity_version
            self.lap.update(fix, now)
            separated = separated or continuity != self.lap.timer.continuity_version
            if self.lap.timer.source_status not in ("late", "duplicate"):
                self.rmc_time = now
                self.position = (fix["latitude"], fix["longitude"])
                tick = fix["timestamp_ms"]
                if self.fix_times and not 0 < elapsed(tick, self.fix_times[-1]) <= GPS_TIMEOUT * 1000:
                    separated = True
                if separated:
                    self.fix_times.clear()
                self.fix_times.append(tick)
                if len(self.map_points) == self.map_points.maxlen:
                    for _ in range(128):
                        self.map_points.popleft()
                self.map_points.append((fix["longitude"], fix["latitude"], values.get("throttle_percent"),
                                        values.get("brake_percent"), separated, tick,
                                        self.pedal_clock.fix_seconds if stamp else None))
                self.last_map_point = self.map_points[-1]
                self.last_valid_arrival = now
                self.map_break = False
                self.sync_status, self.match_offset = status, offset

    def receive_adc(self, frame, now):
        parts = frame.split(b",")
        if len(parts) != 4 or parts[1] not in (b"B", b"T") or not parts[2].isdigit():
            return
        key = parts[1].decode()
        tick = int(parts[2])
        if tick >= MOD or (key == "B" and self.brake_disconnected):
            return
        try:
            counts = None if parts[3] == b"ERR" else int(parts[3])
        except ValueError:
            return
        if counts is not None and not -32768 <= counts <= 32767:
            return
        with self.lock:
            if key == "T":
                display = self.sensor_value(counts)
                pct = display if self.calibration else None
            else:
                volts = None if counts is None else counts * 6.144 / 32768
                display = pct = self.brake_calibration.percent(volts)
            seconds = self.pedal_clock.add(key, tick, pct)
            if seconds is None:
                return
            history = self.history if key == "T" else self.brake_history
            if history and history[-1][0] is not None and seconds - history[-1][0] > 0.1:
                history.append((None, None))
            history.append((seconds, display))
            if key == "T":
                self.counts, self.sensor_time = counts, now
            else:
                self.brake_voltage, self.brake_time, self.brake_seen = volts, now, True
                self.brake_status = "" if pct is not None else "Brake ADC unavailable or outside range"
            self.revision += 1

    def map_snapshot(self):
        with self.lock:
            sample_age = self.lap.sample_age(time.monotonic())
            fresh = self.position is not None and sample_age is not None and sample_age <= GPS_TIMEOUT
            rates = [self.pedal_clock.rate(k) for k in ("T", "B")]
            duration = elapsed(self.fix_times[-1], self.fix_times[0]) if self.fix_times else 0
            gps_rate = (len(self.fix_times) - 1) * 1000 / duration if duration > 0 else None
            detail = self.sync_status
            if self.match_offset is not None:
                detail += f" · max pedal offset {self.match_offset} ms"
            for name, rate in zip(("Throttle", "Brake", "GPS"), [*rates, gps_rate]):
                if rate is not None:
                    detail += f" · {name} {rate:.1f} Hz"
            age = time.monotonic() - self.last_valid_arrival if self.last_valid_arrival is not None else None
            points = list(self.map_points)
            if not points and self.last_map_point is not None:
                points.append(self.last_map_point)
            return points, fresh, detail, self.generation, age

    def receive_throttle(self, frame, now):
        sensor = THROTTLE_FRAME.fullmatch(frame)
        if sensor is None:
            return
        counts = int(sensor[1]) if sensor[1] else None
        if counts is not None and not -32768 <= counts <= 32767:
            return
        with self.lock:
            if self.sensor_time is not None and now - self.sensor_time > SENSOR_TIMEOUT:
                self.history.append((None, None))
                self.revision += 1
            self.counts, self.sensor_time = counts, now
            self.history.append((now - self.origin_time, self.sensor_value(counts)))
            self.revision += 1

    def sensor_value(self, counts):
        if counts is None:
            return None
        millivolts = counts * VOLTS_PER_COUNT * 1000
        return millivolts if self.calibration is None else self.calibration.percent(millivolts)

    def connection_status(self):
        with self.lock:
            return self.status

    def brake_snapshot(self):
        with self.lock:
            if self.brake_disconnected:
                return False, None, "Brake sensor disconnected"
            if self.brake_time is None:
                return self.brake_seen, None, self.brake_status
            if time.monotonic() - self.brake_time > SENSOR_TIMEOUT:
                return self.brake_seen, None, "Brake data stale"
            return self.brake_seen, self.brake_voltage, self.brake_status

    def sensor_snapshot(self):
        with self.lock:
            age = None if self.sensor_time is None else time.monotonic() - self.sensor_time
            status, counts = "Waiting for STM32 throttle data", None
            if age is not None and age <= SENSOR_TIMEOUT:
                counts = self.counts
                status = "ADS1115 unavailable" if counts is None else "Live"
            return self.status, status, counts, age

    def sensor_history(self):
        with self.lock:
            times = [point[0] for point in self.history]
            values = [point[1] for point in self.history]
            return [times, values], self.revision, self.generation

    def pedal_history(self):
        with self.lock:
            throttle = list(self.history) if self.calibration is not None else []
            return throttle, list(self.brake_history), self.revision, self.generation

    def lap_snapshot(self):
        with self.lock:
            now = time.monotonic()
            state = self.lap.snapshot(now)
            status = "Waiting for GPS NMEA"
            if self.nmea_time is not None and now - self.nmea_time <= GPS_TIMEOUT:
                status = "NMEA received · Waiting for RMC"
                if self.rmc_time is not None and now - self.rmc_time <= GPS_TIMEOUT and self.position is None:
                    status = "No GPS fix"
            if state["fresh"]:
                latitude, longitude = self.position
                status = f"GPS fix · {latitude:.6f}°, {longitude:.6f}°"
            else:
                for key in ("current", "current_inputs", "current_channels", "current_distances"):
                    state[key] = []
            state["hardware_gps_status"] = status
            state["hardware_lap_timing"] = self.lap.timer.timing_enabled
            return state

    def clock_state(self):
        with self.lock:
            return self.lap.clock_state()

    def select_reference(self, number):
        with self.lock:
            self.lap.select_reference(number)

    def read(self):
        while not self.stop.is_set():
            try:
                with serial.serial_for_url(self.port, BAUD, timeout=0.25) as connection:
                    connection.reset_input_buffer()
                    self.connection_changed(f"{self.port} connected")
                    pending = b""
                    while not self.stop.is_set():
                        pending += connection.read_until(size=160 - len(pending))
                        if pending.endswith(b"\n"):
                            self.receive(pending)
                            pending = b""
                        elif len(pending) == 160:
                            pending = b""
            except OSError as error:
                self.connection_changed(f"{self.port} unavailable · {error}")
                self.stop.wait(1.0)

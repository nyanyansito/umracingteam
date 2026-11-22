"""Read UMR bench frames over STM32 USB or the ESP32 TCP bridge."""

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
import re
import threading
import time

import serial

from lap_analysis import LapAnalysis
from lap_timer import LapTimer
from telemetry_config import TRACK_ID

BAUD = 115200
VOLTS_PER_COUNT = 4.096 / 32768  # ADS1115 gain setting in the bench sketch.
SENSOR_TIMEOUT = 2.0
GPS_TIMEOUT = 3.0
STEERING_FRAME = re.compile(rb"UMR_STEERING,(-?[0-9]{1,5})?")
THROTTLE_FRAME = re.compile(rb"UMR_THROTTLE,(-?[0-9]{1,5})?")
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


@dataclass(frozen=True)
class SteeringCalibration:
    ccw: float
    center: float
    cw: float
    span: float

    def __post_init__(self):
        if not all(isfinite(value) for value in (self.ccw, self.center, self.cw, self.span)):
            raise ValueError("Calibration values must be finite.")
        if not 0 <= min(self.ccw, self.cw) < self.center < max(self.ccw, self.cw) <= 4.096:
            raise ValueError("Center voltage must lie between the two endpoint voltages.")
        if self.span <= 0:
            raise ValueError("Calibration angle must be positive.")

    def angle(self, volts):
        if not min(self.ccw, self.cw) <= volts <= max(self.ccw, self.cw):
            return None
        if (volts - self.center) * (self.cw - self.center) >= 0:
            return self.span * (volts - self.center) / (self.cw - self.center)
        return -self.span * (volts - self.center) / (self.ccw - self.center)


@dataclass(frozen=True)
class ThrottleCalibration:
    released_mv: float
    pressed_mv: float

    def __post_init__(self):
        if not (self.in_fixture_range(self.released_mv)
                and self.in_fixture_range(self.pressed_mv)
                and self.released_mv != self.pressed_mv):
            raise ValueError("Use two distinct throttle readings between 5 and 250 mV.")

    @staticmethod
    def in_fixture_range(millivolts):
        # The specified divider reads about 300 mV open and 0 mV shorted.
        return 5 < millivolts < 250

    def percent(self, millivolts):
        if not self.in_fixture_range(millivolts):
            return None
        fraction = (millivolts - self.released_mv) / (self.pressed_mv - self.released_mv)
        return 100 * max(0, min(1, fraction))


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
                 track_configured=TRACK_ID != "red_bull_ring"):
        self.port = port
        self.calibration = calibration
        self.throttle_only = throttle_only
        self.sensor_frame = THROTTLE_FRAME if throttle_only else STEERING_FRAME
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.origin_time = time.monotonic()
        self.lap = LapAnalysis(LapTimer(maximum_gap_seconds=GPS_TIMEOUT, timing_enabled=track_configured))
        self.history = deque(maxlen=1000)
        self.generation = 0
        self.connection_changed(f"Opening {port}…")

    def connection_changed(self, status):
        with self.lock:
            self.status = status
            self.counts = None
            self.sensor_time = None
            self.nmea_time = None
            self.rmc_time = None
            self.position = None
            self.history.clear()
            self.generation += 1
            self.revision = 0
            self.lap.interrupt()

    def receive(self, line):
        if not line.endswith(b"\n"):
            return  # Incomplete frame, or a line longer than the read limit.
        frame = line.rstrip(b"\r\n")
        now = time.monotonic()
        sensor = self.sensor_frame.fullmatch(frame)
        if sensor:
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
            return

        if self.throttle_only:
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
                return
            if self.counts is not None and self.calibration is not None and now - self.sensor_time <= .15:
                fix["steering_degrees"] = self.calibration.angle(self.counts * VOLTS_PER_COUNT)
            # The GPS source clock drives lap timing. A host-side outage must
            # still break the trace, even if a receiver replays a recent fix.
            if self.rmc_time is not None and now - self.rmc_time > GPS_TIMEOUT:
                self.lap.interrupt()
            self.lap.update(fix, now)
            if self.lap.timer.source_status not in ("late", "duplicate"):
                self.rmc_time = now
                self.position = (fix["latitude"], fix["longitude"])

    def sensor_value(self, counts):
        if counts is None:
            return None
        volts = counts * VOLTS_PER_COUNT
        if self.throttle_only:
            millivolts = volts * 1000
            return millivolts if self.calibration is None else self.calibration.percent(millivolts)
        return volts if self.calibration is None else self.calibration.angle(volts)

    def sensor_snapshot(self):
        with self.lock:
            age = None if self.sensor_time is None else time.monotonic() - self.sensor_time
            sensor = "throttle" if self.throttle_only else "steering"
            status, counts = f"Waiting for STM32 {sensor} data", None
            if age is not None and age <= SENSOR_TIMEOUT:
                counts = self.counts
                status = "ADS1115 unavailable" if counts is None else "Live"
            return self.status, status, counts, age

    def sensor_history(self):
        with self.lock:
            times = [point[0] for point in self.history]
            values = [point[1] for point in self.history]
            unavailable = [None] * len(times)
            throttle, steering = (values, unavailable) if self.throttle_only else (unavailable, values)
            return [times, throttle, unavailable, steering, unavailable], self.revision, self.generation

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

"""Read the steering/GPS bench sketch directly from the STM32 USB serial port."""

from collections import deque
from dataclasses import dataclass
from math import isfinite
import re
import threading
import time

from dash import Dash, Input, Output, dcc, html
import plotly.graph_objects as go
import serial

BAUD = 115200
VOLTS_PER_COUNT = 4.096 / 32768  # ADS1115 gain setting in the bench sketch.
STEERING_TIMEOUT = 2.0
GPS_TIMEOUT = 3.0
STEERING_FRAME = re.compile(rb"UMR_STEERING,(-?[0-9]{1,5})?")
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


class SerialBench:
    def __init__(self, port):
        self.port = port
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.connection_changed(f"Opening {port}…")

    def connection_changed(self, status):
        with self.lock:
            self.status = status
            self.counts = None
            self.steering_time = None
            self.nmea_time = None
            self.rmc_time = None
            self.trail = deque(maxlen=300)

    def receive(self, line):
        if not line.endswith(b"\n"):
            return  # Incomplete frame, or a line longer than the read limit.
        frame = line.rstrip(b"\r\n")
        now = time.monotonic()
        steering = STEERING_FRAME.fullmatch(frame)
        if steering:
            counts = int(steering[1]) if steering[1] else None
            if counts is not None and not -32768 <= counts <= 32767:
                return
            with self.lock:
                self.counts, self.steering_time = counts, now
            return

        fields = checked_nmea(frame)
        if fields is None:
            return
        with self.lock:
            self.nmea_time = now
            if fields[0][2:] == "RMC":
                position = rmc_position(fields)
                if self.rmc_time is None or now - self.rmc_time > GPS_TIMEOUT or position is None:
                    self.trail.clear()
                self.rmc_time = now
                if position is not None:
                    self.trail.append(position)

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            steering_status = "Waiting for STM32 steering data"
            counts = None
            if self.steering_time is not None and now - self.steering_time <= STEERING_TIMEOUT:
                counts = self.counts
                steering_status = "ADS1115 unavailable" if counts is None else "Live"

            gps_status = "Waiting for GPS NMEA"
            trail = ()
            if self.nmea_time is not None and now - self.nmea_time <= GPS_TIMEOUT:
                gps_status = "NMEA received · Waiting for RMC"
                if self.rmc_time is not None and now - self.rmc_time <= GPS_TIMEOUT:
                    trail = tuple(self.trail)
                    gps_status = "GPS fix" if trail else "No GPS fix"

            return self.status, steering_status, counts, gps_status, trail

    def read(self):
        while not self.stop.is_set():
            try:
                with serial.Serial(self.port, BAUD, timeout=0.25) as connection:
                    connection.reset_input_buffer()
                    self.connection_changed(f"{self.port} connected · {BAUD} baud")
                    while not self.stop.is_set():
                        self.receive(connection.read_until(size=160))
            except OSError as error:
                self.connection_changed(f"{self.port} unavailable · {error}")
                self.stop.wait(1.0)


def run_dashboard(port, calibration, host, dashboard_port):
    bench = SerialBench(port)
    app = Dash(__name__, title="UMR Hardware Bench")

    def card(title, *children):
        return html.Section([html.H2(title), *children], style={
            "padding": "1rem 1.5rem", "border": "1px solid #dce2e9",
            "borderRadius": "12px", "background": "#ffffff",
        })

    app.layout = html.Main([
        html.H1("UMR Hardware Bench"),
        html.P(id="serial-status"),
        card("Steering", html.Div(id="steering-reading")),
        card("GPS", html.Div(id="gps-reading"),
             dcc.Graph(id="gps-trace", config={"displayModeBar": False})),
        dcc.Interval(id="bench-refresh", interval=250),
    ], style={"maxWidth": "960px", "margin": "2rem auto", "padding": "0 1rem",
              "fontFamily": "system-ui, sans-serif", "display": "grid", "gap": "0.75rem"})

    @app.callback(Output("serial-status", "children"), Output("steering-reading", "children"),
                  Output("gps-reading", "children"), Output("gps-trace", "figure"),
                  Input("bench-refresh", "n_intervals"))
    def refresh(_):
        status, steering_status, counts, gps_status, trail = bench.snapshot()
        steering_readout = steering_status
        if counts is not None:
            volts = counts * VOLTS_PER_COUNT
            angle_label = "Angle calibration required"
            if calibration is not None:
                angle = calibration.angle(volts)
                angle_label = "Outside calibration range" if angle is None else f"{angle:+.1f}°"
            steering_readout = f"{angle_label} · {volts:.4f} V · {counts} ADC counts"

        figure = go.Figure()
        gps_readout = gps_status
        if trail:
            latitude, longitude = trail[-1]
            gps_readout = f"{gps_status} · {latitude:.6f}°, {longitude:.6f}°"
            latitudes, longitudes = zip(*trail)
            figure.add_scatter(x=longitudes, y=latitudes, mode="lines+markers",
                               marker={"size": 5, "color": "#1665a9"})
        figure.update_layout(xaxis_title="Longitude (°)", yaxis_title="Latitude (°)",
                             height=320, margin={"l": 55, "r": 20, "t": 15, "b": 55})
        return status, steering_readout, gps_readout, figure

    reader = threading.Thread(target=bench.read, name="stm32-usb", daemon=True)
    reader.start()
    try:
        app.run(host=host, port=dashboard_port, debug=False)
    finally:
        bench.stop.set()
        reader.join()

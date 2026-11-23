import argparse
import threading
import time
import json
from collections import deque
from pathlib import Path
from urllib.parse import quote

import paho.mqtt.client as mqtt
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, Patch, ctx, no_update, dcc, html, ClientsideFunction
from plotly.subplots import make_subplots

# ADDED: keeps out-of-range warnings visible for a few seconds
from flag_alerts import AlertTracker, describe_alert
from mqtt_runtime import start_mqtt
from lap_analysis import LapAnalysis
from lap_timer import GPS_TOPIC
from telemetry_validation import FAST_FIELDS, decode_object, is_number, validate_fast_sample
from telemetry_config import BROKER, PORT, DASHBOARD_HOST, DASHBOARD_PORT
from sample_order import SampleOrder, sample_key
from data_processing import CHANNEL_SPECS
from telemetry_views import DASHBOARD_FIELDS, THROTTLE_GREEN, lap_input_figure, stream_figure
from lap_stream import RemoteLapAnalysis, STATE_TOPIC, UPDATE_TOPIC, REQUEST_TOPIC, REFERENCE_TOPIC


TOPIC = "umf1/car01/telemetry/fast"
THROTTLE_CALIBRATION_DIR = Path(__file__).resolve().parent / "logs" / "throttle-calibration"

# ADDED: validated copy of the fast telemetry, published by data_processing.py
PROCESSED_TOPIC = "umf1/car01/telemetry/fast/processed"

telemetry = {
    "sequence": 0,
    "timestamp_ms": 0,
    "throttle_percent": 0,
    "brake_bar": 0,
    "steering_degrees": 0,
    "speed_kmh": 0,
}

telemetry_lock = threading.Lock()
FRESHNESS_SECONDS = 2.0
GATEWAY_STATUS_TOPIC = "umf1/car01/gateway/status"
GATEWAY_FRESHNESS_SECONDS = 3.0
last_raw_time = None
raw_invalid = False
mqtt_connected = False
gateway_status = None
last_gateway_time = None

time_history = deque(maxlen=1000)
throttle_history = deque(maxlen=1000)
brake_history = deque(maxlen=1000)
steering_history = deque(maxlen=1000)
speed_history = deque(maxlen=1000)
sample_order = SampleOrder()
history_revision = 0
history_generation = 0
processed_key = None
source_offset = None
receive_times = deque(maxlen=200)
last_snapshot_request = -float("inf")

# ADDED: remembers which channels were recently flagged out of range
alert_tracker = AlertTracker()
lap_analysis = RemoteLapAnalysis()
hardware_source = None


def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    with telemetry_lock:
        mqtt_connected = reason_code == 0
    if reason_code == 0:
        client.subscribe(TOPIC)
        client.subscribe(PROCESSED_TOPIC)
        client.subscribe(GATEWAY_STATUS_TOPIC, qos=1)
        client.subscribe(STATE_TOPIC, qos=1)
        client.subscribe(UPDATE_TOPIC)
        client.publish(REQUEST_TOPIC, "{}", qos=1)


def on_disconnect(client, userdata, flags, reason_code, properties):
    global mqtt_connected, last_raw_time
    with telemetry_lock:
        mqtt_connected = False
        last_raw_time = None
        alert_tracker.last_processed_time = None
        lap_analysis.interrupt()


def on_gps_message(client, userdata, message):
    try:
        gps = decode_object(message.payload)
        if getattr(message, "retain", False):
            return
    except (ValueError, UnicodeDecodeError):
        with telemetry_lock:
            lap_analysis.interrupt()
        return
    with telemetry_lock:
        if isinstance(lap_analysis, LapAnalysis):
            lap_analysis.update(gps)


def on_lap_state(client, userdata, message):
    global last_snapshot_request
    if not isinstance(lap_analysis, RemoteLapAnalysis):
        return
    try:
        with telemetry_lock:
            lap_analysis.receive(decode_object(message.payload), retained=getattr(message, "retain", False))
    except (ValueError, UnicodeDecodeError, TypeError, KeyError):
        with telemetry_lock:
            lap_analysis.interrupt()
    now = time.monotonic()
    if lap_analysis.needs_snapshot and now - last_snapshot_request > 1:
        last_snapshot_request = now
        client.publish(REQUEST_TOPIC, "{}", qos=1)


def on_message(client, userdata, message):
    global last_raw_time, raw_invalid, history_revision, history_generation, processed_key, source_offset
    try:
        received = validate_fast_sample(decode_object(message.payload))
        if getattr(message, "retain", False):
            raise ValueError("Retained telemetry is not a live sample")
        # Extract everything before changing any shared state.
        sample = (received["timestamp_ms"] / 1000,
                  *(received[field] for field in FAST_FIELDS))
    except (ValueError, UnicodeDecodeError):
        with telemetry_lock:
            raw_invalid = True
        print("Dashboard received an invalid message")
        return
    with telemetry_lock:
        previous_timestamp = sample_order.timestamp
        order = sample_order.check(received)
        if order in ("late", "duplicate"):
            return
        histories = (time_history, throttle_history, brake_history, steering_history, speed_history)
        if order in ("first", "reset"):
            for history in histories:
                history.clear()
            history_generation += 1
            history_revision = 0
            source_offset = None
            processed_key = None
            receive_times.clear()
            alert_tracker.latest_flags.clear()
            alert_tracker._alerts.clear()
        elif previous_timestamp is not None and (received["timestamp_ms"] - previous_timestamp) % (1 << 32) > 100:
            for history in histories:
                history.append(None)
            history_revision += 1
        sample = (sample_order.unwrapped_ms / 1000, *sample[1:])
        telemetry.clear()
        telemetry.update(received)
        for history, value in zip((time_history, throttle_history, brake_history,
                                   steering_history, speed_history), sample):
            history.append(value)
        last_raw_time = time.monotonic()
        offset = last_raw_time - sample_order.unwrapped_ms / 1000
        source_offset = offset if source_offset is None else min(source_offset, offset)
        receive_times.append(last_raw_time)
        history_revision += 1
        raw_invalid = False


def on_processed_message(client, userdata, message):
    global processed_key
    try:
        processed = decode_object(message.payload)
        flags, values, limits = (processed.get(key) for key in ("range_flags", "values", "limits"))
        if (not all(isinstance(item, dict) for item in (flags, values, limits))
                or not set(FAST_FIELDS).issubset(flags)
                or getattr(message, "retain", False)):
            raise ValueError("Incomplete validation result")
        for field, flag in flags.items():
            if flag not in ("ok", "out_of_range", "invalid", "missing"):
                raise ValueError("Unknown validation flag")
            if field in FAST_FIELDS and flag in ("ok", "out_of_range") and not is_number(values.get(field)):
                raise ValueError("Invalid validated value")
            bounds = limits.get(field, {})
            if not isinstance(bounds, dict) or any(
                    bound is not None and not is_number(bound)
                    for bound in (bounds.get("min"), bounds.get("max"))):
                raise ValueError("Invalid limits")
        with telemetry_lock:
            # Late validation can raise a warning, but only matching validation
            # is allowed to certify the values currently displayed.
            if processed.get("session_id") == telemetry.get("session_id"):
                alert_tracker.update(processed)
                processed_key = sample_key(processed)
    except (ValueError, UnicodeDecodeError):
        print("Dashboard received an invalid processed message")


def on_gateway_status(client, userdata, message):
    global gateway_status, last_gateway_time
    try:
        status = decode_object(message.payload)
        if (type(status.get("online")) is not bool
                or type(status.get("stm32_connected")) is not bool
                or type(status.get("telemetry_fresh")) is not bool):
            raise ValueError("Invalid gateway status")
        with telemetry_lock:
            gateway_status = status
            # A retained heartbeat alone is not evidence of a live gateway.
            last_gateway_time = None if getattr(message, "retain", False) else time.monotonic()
    except (ValueError, UnicodeDecodeError):
        print("Dashboard received invalid gateway status")


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="umf1-dashboard")
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message
mqtt_client.message_callback_add(PROCESSED_TOPIC, on_processed_message)
mqtt_client.message_callback_add(GATEWAY_STATUS_TOPIC, on_gateway_status)
mqtt_client.message_callback_add(GPS_TOPIC, on_gps_message)
mqtt_client.message_callback_add(STATE_TOPIC, on_lap_state)
mqtt_client.message_callback_add(UPDATE_TOPIC, on_lap_state)


app = Dash(__name__, title="UM Racing · Pit Wall")
logo_asset = "team-logo.png"
team_logo = (html.Img(src=app.get_asset_url(logo_asset), alt="UM Racing logo")
             if (Path(app.config.assets_folder) / logo_asset).is_file()
             else html.Span("UMR", **{"aria-hidden": "true"}))

box_style = {"color": "var(--text)"}
plot_style = {
    "paper_bgcolor": "#121720",
    "plot_bgcolor": "#0e131c",
    "font": {"family": "Bahnschrift, Segoe UI, system-ui, sans-serif", "color": "#b6c3d6"},
}

# ADDED: (component id prefix, wire field name, title shown on the box)
METRIC_BOXES = [
    ("throttle", "throttle_percent", "Throttle"),
    ("brake", "brake_bar", "Brake Pressure"),
    ("speed", "speed_kmh", "Vehicle Speed"),
]

alert_box_style = {
    **box_style,
    "backgroundColor": "#2c171f",
    "borderColor": "#ac4357",
}


def make_metric_box(key, title):
    return html.Div(
        [
            html.H3(title, id=f"{key}-title", className="metric-label"),
            html.H2(id=f"{key}-value", className="metric-value"),
            html.P(id=f"{key}-warning", className="metric-detail"),
        ],
        id=f"{key}-box",
        className=f"metric-card metric-card--{key}",
        style=box_style,
    )


app.layout = html.Div(
    [
        html.Header([
            html.Div(team_logo, className="team-mark"),
            html.Div([
                html.P("UM Racing / Car 01", className="team-name"),
                html.H1(["Pit Wall", html.Span("Live telemetry")]),
            ]),
        ], className="pit-header"),
        html.Div([
            html.Div(id="validation-status", className="validation-status"),
            html.P(id="sequence-value", className="packet-status"),
        ], className="status-strip"),
        html.Div([make_metric_box(key, title) for key, _, title in METRIC_BOXES],
                 className="sensor-metrics"),
        html.Div([
            html.Div([html.Small("LAP TIME · LIVE", className="metric-label"),
                      html.Div("—", id="lap-live-clock", className="metric-value", **{"data-lap-clock": "true"})],
                     className="metric-card metric-card--clock"),
            html.Div(id="lap-metrics", className="lap-metrics"),
        ], className="lap-metrics-grid"),
        dcc.Store(id="lap-clock-state"),
        dcc.Store(id="lap-clock-ack"),
        html.P(id="lap-status", className="lap-status"),
        html.Div([
            html.Label(["Reference lap", dcc.Dropdown(id="reference-choice", value="best", clearable=False,
                        options=[{"label": "Session best (automatic)", "value": "best"}], className="pit-select")]),
            html.Label(["Track colour", dcc.Dropdown(id="track-colour", value="inputs", clearable=False,
                        options=[{"label": label, "value": value} for label, value in
                                 (("Pedal activity", "inputs"), ("Speed", "speed_kmh"), ("Throttle", "throttle_percent"), ("Brake pressure", "brake_bar"))],
                        className="pit-select")]),
            html.Button("Return to live", id="return-live", n_clicks=0),
            html.Span(id="reference-feedback"),
        ], className="pit-controls"),
        dcc.Store(id="inspection"), dcc.Store(id="input-view-state"),
        dcc.Store(id="sensor-view-state"), dcc.Store(id="lap-view-state"),
        html.Div([
            dcc.Graph(id="track-trace", className="graph-panel"),
            html.Div([html.H3("Session laps"), html.Div(id="lap-history")],
                     className="lap-panel"),
        ], className="track-grid"),
        dcc.Graph(id="lap-delta-graph", className="graph-panel"),
        dcc.Graph(id="telemetry-graphs", className="graph-panel"),
        dcc.Graph(id="lap-input-graphs", className="graph-panel"),

        dcc.Interval(
            id="dashboard-update",
            interval=200,
            n_intervals=0,
        ),
        dcc.Interval(id="lap-update", interval=200, n_intervals=0),
        dcc.Interval(id="clock-update", interval=100, n_intervals=0),
        dcc.Interval(id="sensor-update", interval=100, n_intervals=0),
    ],
    className="pit-wall",
)


def lap_time(seconds):
    if seconds is None:
        return "—"
    milliseconds = round(seconds * 1000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    return f"{minutes}:{milliseconds / 1000:06.3f}"


def lap_figures(state, colour="inputs", inspection=None, live_revision=0):
    figure = go.Figure()
    outline = state.get("track_outline", [])
    if outline:
        figure.add_trace(go.Scatter(x=[p[0] for p in outline], y=[p[1] for p in outline],
                                   mode="lines", name="Circuit", line={"color": "#334155", "width": 12}, hoverinfo="skip"))
    reference, current = state["reference"], state["current"]
    if reference:
        figure.add_trace(go.Scatter(x=[p[0] for p in reference], y=[p[1] for p in reference],
                                   customdata=[p[3] for p in reference], mode="lines", name="Reference lap",
                                   line={"color": "#94a3b8", "width": 9, "dash": "dot"},
                                   hovertemplate="Reference · %{customdata:.0f} m<extra></extra>"))
    distances = state.get("current_distances", [])
    modes = state.get("current_inputs", [])
    colors = {"throttle": ("Throttle", THROTTLE_GREEN), "brake": ("Braking", "#ef4444"),
              "coast": ("Coasting", "#a3a3a3"), "unknown": ("No input data", "#475569")}
    if colour == "inputs":
        for mode, (name, color) in colors.items():
            xs, ys, ds = [], [], []
            for i in range(1, len(current)):
                segment_mode = modes[i - 1] if i - 1 < len(modes) else "unknown"
                if segment_mode == mode:
                    xs.extend((current[i - 1][0], current[i][0], None))
                    ys.extend((current[i - 1][1], current[i][1], None))
                    ds.extend((distances[i - 1] if i - 1 < len(distances) else None,
                               distances[i] if i < len(distances) else None, None))
            if xs:
                figure.add_trace(go.Scatter(x=xs, y=ys, customdata=ds, mode="lines", name=name,
                                           line={"color": color, "width": 5}, connectgaps=False,
                                           hovertemplate=name + " · %{customdata:.0f} m<extra></extra>"))
    else:
        figure.add_trace(go.Scatter(x=[p[0] for p in current], y=[p[1] for p in current],
                                   mode="lines", name="Current lap", line={"color": "#475569", "width": 5}, hoverinfo="skip"))
        channels = state.get("current_channels", [])
        channel_scale = [[0, "#16422B"], [1, THROTTLE_GREEN]] if colour == "throttle_percent" else "Turbo"
        indices = [i for i, sample in enumerate(channels) if i < len(current) and is_number(sample.get(colour))]
        figure.add_trace(go.Scatter(x=[current[i][0] for i in indices], y=[current[i][1] for i in indices],
                                   customdata=[distances[i] if i < len(distances) else None for i in indices],
                                   mode="markers", name=colour.replace("_", " "),
                                   marker={"size": 7, "color": [channels[i][colour] for i in indices],
                                           "colorscale": channel_scale, "showscale": True,
                                           "colorbar": {"title": "km/h" if colour == "speed_kmh" else "%" if colour == "throttle_percent" else "bar"}},
                                   hovertemplate="%{marker.color:.1f} · %{customdata:.0f} m<extra></extra>"))
    if current:
        figure.add_trace(go.Scatter(x=[current[-1][0]], y=[current[-1][1]], mode="markers",
                                   name="Car" if state["fresh"] else "Last position", hoverinfo="skip",
                                   marker={"color": "#34d399" if state["fresh"] else "#94a3b8", "size": 12}))
    if is_number(inspection):
        candidates = [(abs(d - inspection), p) for d, p in zip(distances, current) if is_number(d)]
        if not candidates:
            candidates = [(abs(p[3] - inspection), p) for p in reference]
        if candidates:
            point = min(candidates, key=lambda item: item[0])[1]
            figure.add_trace(go.Scatter(x=[point[0]], y=[point[1]], mode="markers", name="Inspection",
                                       marker={"size": 16, "color": "#f8fafc", "symbol": "circle-open", "line": {"width": 3}}, hoverinfo="skip"))
    figure.update_layout(**plot_style, template="plotly_dark",
                         title="Red Bull Ring · Austria" if outline else "GPS track trace", height=420, uirevision=f"track:{live_revision}",
                         xaxis_title="East (m)", yaxis_title="North (m)",
                         yaxis={"scaleanchor": "x", "scaleratio": 1}, margin={"t": 55, "b": 45, "l": 55, "r": 20})
    if not current and not reference:
        figure.add_annotation(text="Waiting for a valid GPS fix", xref="paper", yref="paper", x=.5, y=.5, showarrow=False)
    delta = go.Figure(go.Scatter(x=[p[0] for p in state["delta_trace"]],
                                y=[p[1] for p in state["delta_trace"]], mode="lines", connectgaps=False,
                                line={"color": "#38bdf8"}, name="Delta"))
    delta.add_hline(y=0, line_color="#64748b")
    if is_number(inspection):
        delta.add_vline(x=inspection, line_color="#f8fafc", line_dash="dot")
    delta.update_layout(**plot_style, template="plotly_dark",
                        title="Live delta vs reference lap · negative is faster", height=230,
                        uirevision=f"delta:{live_revision}", xaxis_title="Distance along reference lap (m)", yaxis_title="Delta (s)",
                        margin={"t": 55, "b": 45, "l": 65, "r": 20})
    return figure, delta


def lap_snapshot():
    if hardware_source is not None:
        return hardware_source.lap_snapshot()
    with telemetry_lock:
        return lap_analysis.snapshot()


def update_lap_dashboard(_, colour="inputs", inspection=None, live_revision=0, state=None):
    if state is None:
        state = lap_snapshot()
    delta = state["delta"]
    metrics = [("CURRENT LAP", str(state["lap_number"] or "—")),
               ("LAST LAP", lap_time(state["last"])),
               ("BEST LAP", lap_time(state["best"])),
               ("LIVE DELTA", "—" if delta is None else f"{delta:+.3f} s"),
               ("SESSION TOP SPEED", speed_text(state["session_top_speed"]) + " km/h")]
    cards = [html.Div([html.Small(label, className="metric-label"),
                      html.Div(value, className="metric-value",
                               style={"color": ("#34d399" if delta <= 0 else "#fb7185") if label == "LIVE DELTA" and delta is not None else "var(--text)"})],
                     className="metric-card")
             for label, value in metrics]
    if not state["fresh"]:
        status = "GPS unavailable or stale · showing last known lap time"
    elif state["elapsed"] is None:
        status = "Waiting for the next finish-line crossing to start a complete lap"
    elif not state["reference"]:
        status = "Recording the first complete lap · delta becomes available after completion"
    elif delta is None:
        status = "Position cannot be matched reliably to the reference · delta unavailable"
    else:
        status = f"Comparing with {"best" if state.get("selected_reference") is None else "reference"} lap {state['reference_lap']} · negative delta means faster"
    if "hardware_gps_status" in state:
        gps_status = state["hardware_gps_status"]
        if not state["fresh"]:
            status = gps_status
        elif not state["hardware_lap_timing"]:
            status = gps_status + " · Configure a local circuit to enable lap timing"
        else:
            status = gps_status + " · " + status
    if state.get("invalid_reason"):
        status += " · Last crossing rejected: " + state["invalid_reason"]
    if state.get("gps_age") is not None:
        status += f" · GPS age {state['gps_age'] * 1000:.0f} ms"
    history = lap_table(state)
    track, delta_figure = lap_figures(state, colour, inspection, live_revision)
    return cards, status, track, delta_figure, history


@app.callback(Output("lap-clock-state", "data"), Input("clock-update", "n_intervals"),
              running=[(Output("clock-update", "disabled"), True, False)])
def update_lap_clock(_):
    if hardware_source is not None:
        return hardware_source.clock_state()
    with telemetry_lock:
        return lap_analysis.clock_state()


app.clientside_callback(ClientsideFunction(namespace="telemetryClock", function_name="update"),
                        Output("lap-clock-ack", "data"), Input("lap-clock-state", "data"))


def speed_text(value):
    return "—" if value is None else f"{value:.0f}"


def lap_table(state):
    def row(number, seconds, speed, difference, best=False, live=False):
        label = "BEST" if best else "—" if difference is None else f"{difference:+.3f}"
        color = "#4ade80" if best or (difference is not None and difference <= .1) else "#fbbf24" if difference is not None and difference < 1 else "#fb7185"
        return html.Tr([
            html.Td(number),
            html.Td(lap_time(seconds), **({"data-lap-clock": "true"} if live else {})),
            html.Td(speed_text(speed)),
            html.Td(label, style={"color": color}),
        ], className="lap-best" if best else "lap-live" if live else "")
    rows = [row("SESSION BEST", state["best"], state["best_top_speed"], None, best=state["best"] is not None)]
    for lap in state["history"]:
        # Recompute against the CURRENT session best, so every row uses
        # one consistent reference when a new fastest lap is completed.
        rows.append(row(str(lap["lap_number"]), lap["lap_time_seconds"], lap["top_speed_kmh"],
                        lap["lap_time_seconds"] - state["best"], lap["lap_number"] == state.get("best_lap", state["reference_lap"])))
    if state["lap_number"] is not None:
        rows.append(row(f"{state['lap_number']} LIVE", state["elapsed"], state["lap_top_speed"], state["delta"], live=True))
    return html.Div([
        html.Div([html.Strong("Whole stint"), html.Span(f"{state['completed_count']} laps")], className="stint-summary"),
        html.P("GPS top speed · completed-lap delta vs session best", className="lap-table-note"),
        html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label) for label in ("#", "TIME", "TOP KPH", "DELTA")])),
            html.Tbody(rows),
        ], className="lap-table"), className="lap-table-scroll"),
    ])


def channel_range(field, values, *, throttle_millivolts=False):
    spec = CHANNEL_SPECS[field]
    low, high = spec["min_value"], spec["max_value"]
    if field == "throttle_percent" and throttle_millivolts:
        low, high = 0, 120
    finite = [v for v in values if is_number(v)]
    low = min([low] + finite)
    high = max([high] + finite)
    return [low, high if high > low else low + 1]


def make_sensor_figure(graph_times, graph_throttle, graph_brake, graph_speed):
    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.07,
        subplot_titles=(
            "Throttle Position",
            "Brake Pressure",
            "Vehicle Speed",
        ),
    )

    figure.add_trace(
        go.Scatter(
            x=graph_times,
            y=graph_throttle,
            mode="lines",
            name="Throttle",
            line={"color": THROTTLE_GREEN},
        ),
        row=1,
        col=1,
    )

    figure.add_trace(
        go.Scatter(
            x=graph_times,
            y=graph_brake,
            mode="lines",
            name="Brake",
            line={"color": "#ef4444"},
        ),
        row=2,
        col=1,
    )

    figure.add_trace(
        go.Scatter(
            x=graph_times,
            y=graph_speed,
            mode="lines",
            name="Speed",
            line={"color": "#38bdf8"},
        ),
        row=3,
        col=1,
    )

    figure.update_yaxes(
        title_text="Throttle (%)",
        range=channel_range("throttle_percent", graph_throttle),
        row=1,
        col=1,
    )

    figure.update_yaxes(
        title_text="Brake (bar)",
        range=channel_range("brake_bar", graph_brake),
        row=2,
        col=1,
    )

    figure.update_yaxes(
        title_text="Speed (km/h)",
        range=channel_range("speed_kmh", graph_speed),
        row=3,
        col=1,
    )

    figure.update_xaxes(
        title_text="Time (seconds)",
        row=3,
        col=1,
    )

    figure.update_layout(
        **plot_style,
        template="plotly_dark",
        height=1000,
        showlegend=False,
        title="Live Telemetry History",
        uirevision="sensors",
    )

    return figure


def make_throttle_bench_figure(times, values, *, millivolts):
    return go.Figure(
        data=[go.Scatter(x=times, y=values, mode="lines", line={"color": THROTTLE_GREEN})],
        layout={
            **plot_style,
            "template": "plotly_dark",
            "title": "Throttle Bench History",
            "xaxis_title": "Time (seconds)",
            "yaxis_title": "Throttle (mV)" if millivolts else "Throttle (%)",
            "yaxis_range": channel_range("throttle_percent", values, throttle_millivolts=millivolts),
        },
    )


def make_live_pedal_figure(throttle, brake):
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08)
    for row, name, color, samples in (
        (1, "Throttle", THROTTLE_GREEN, throttle),
        (2, "Brake", "#f87171", brake),
    ):
        times, values = zip(*samples) if samples else ([], [])
        figure.add_trace(go.Scatter(
            x=times, y=values, name=name, mode="lines+markers",
            line={"color": color, "width": 2}, marker={"size": 3}, connectgaps=False,
            hovertemplate="%{x:.2f} s · %{y:.1f}%<extra>" + name + "</extra>",
        ), row=row, col=1)
        figure.update_yaxes(title_text=f"{name} (%)", range=[0, 100], dtick=25, row=row, col=1)
    figure.update_xaxes(title_text="Elapsed time (s)", row=2, col=1)
    figure.update_layout(**plot_style, template="plotly_dark",
                         title="Live Pedal Inputs", height=500, showlegend=False,
                         margin={"l": 65, "r": 28, "t": 55, "b": 55})
    return figure


def update_hardware_cards(include_figure):
    from hardware_bench import ThrottleCalibration, VOLTS_PER_COUNT

    source = hardware_source
    throttle_only = source.throttle_only
    value, detail, age_label = "—", "", ""
    brake_value, brake_detail, brake_style = "—", "", box_style
    figure = None
    if throttle_only:
        status, detail, counts, age = source.sensor_snapshot()
        calibration = source.calibration
        if counts is not None:
            millivolts = counts * VOLTS_PER_COUNT * 1000
            percent = None if calibration is None else calibration.percent(millivolts)
            value = f"{millivolts:.1f} mV" if percent is None else f"{percent:.1f} %"
            detail = f"{millivolts:.1f} mV · {counts} ADC counts"
            if not ThrottleCalibration.in_fixture_range(millivolts):
                detail += " · Check pedal and resistor connections"
            elif percent is None:
                detail += " · Calibration required for %"
        if age is not None:
            age_label = f" · last throttle frame {age * 1000:.0f} ms ago"
        if include_figure:
            arrays, _, _ = source.sensor_history()
            figure = make_throttle_bench_figure(*arrays, millivolts=calibration is None)
    else:
        status, detail, counts, _ = source.sensor_snapshot()
        if counts is not None:
            millivolts = counts * VOLTS_PER_COUNT * 1000
            percent = None if source.calibration is None else source.calibration.percent(millivolts)
            if percent is None:
                reason = ("Calibration required" if ThrottleCalibration.in_fixture_range(millivolts)
                          else "Throttle signal out of range")
                detail = f"{millivolts:.1f} mV · {reason}"
            else:
                value, detail = f"{percent:.1f} %", f"{millivolts:.1f} mV"
        _, brake_volts, brake_detail = source.brake_snapshot()
        if brake_detail and brake_detail != "Waiting for brake data":
            brake_style = alert_box_style
        if brake_volts is not None:
            percent = source.brake_calibration.percent(brake_volts)
            if percent is not None:
                brake_value = f"{percent:.1f} %"
            brake_detail = " · ".join(filter(None, (f"{brake_volts:.3f} V", brake_detail)))
        if include_figure:
            throttle, brake, _, _ = source.pedal_history()
            figure = make_live_pedal_figure(throttle, brake)

    outputs = []
    for key, _, title in METRIC_BOXES:
        if key == "throttle":
            outputs += ["Throttle Bench" if throttle_only else "Throttle Input", box_style, detail]
        elif not throttle_only and key == "brake":
            outputs += ["Brake Input", brake_style, brake_detail]
        else:
            outputs += [title, {"display": "none"}, ""]
    link = "ESP32 Wi-Fi" if source.port.startswith("socket://") else "STM32 USB"
    label = "Hardware data · throttle bench" if throttle_only else "Hardware data · GPS and pedal inputs"
    return (value, brake_value, "—", f"{link} · {status}{age_label}", figure, *outputs, label, {
                "backgroundColor": "#152539", "color": "#bfdbfe",
            })


def update_dashboard(_, include_figure=True):
    if hardware_source is not None:
        return update_hardware_cards(include_figure)
    with telemetry_lock:
        latest = telemetry.copy()

        graph_times = list(time_history)
        graph_throttle = list(throttle_history)
        graph_brake = list(brake_history)
        graph_speed = list(speed_history)

        # ADDED: snapshot of warnings that are still active
        active_alerts = {field: alert for field, alert in alert_tracker.active().items()
                         if field in DASHBOARD_FIELDS}
        now = time.monotonic()
        age = None if last_raw_time is None else max(0, now - last_raw_time,
                     0 if source_offset is None else now - sample_order.unwrapped_ms / 1000 - source_offset)
        raw_fresh = age is not None and age <= FRESHNESS_SECONDS
        age_text = "—" if age is None else f"{age * 1000:.0f} ms"
        rate = ((len(receive_times) - 1) / (receive_times[-1] - receive_times[0])
                if raw_fresh and len(receive_times) > 1 and receive_times[-1] > receive_times[0] else 0)
        missed, rejected = sample_order.missed, sample_order.rejected
        matching_validation = processed_key == sample_key(latest)
        processed_fresh = (alert_tracker.last_processed_time is not None
                           and now - alert_tracker.last_processed_time <= FRESHNESS_SECONDS)
        current_errors = {field for field in DASHBOARD_FIELDS
                          if alert_tracker.latest_flags.get(field, "ok") != "ok"}
        connected = mqtt_connected
        invalid_sample = raw_invalid
        status = gateway_status.copy() if gateway_status else None
        gateway_fresh = last_gateway_time is not None and now - last_gateway_time <= GATEWAY_FRESHNESS_SECONDS

    figure = make_sensor_figure(graph_times, graph_throttle, graph_brake, graph_speed) if include_figure else None

    # ADDED: build the per-box warning outputs (title, style, text).
    alert_outputs = []

    for key, field_name, title in METRIC_BOXES:
        alert = active_alerts.get(field_name)

        if alert is None:
            alert_outputs += [title, box_style, ""]
        else:
            alert_outputs += [
                f"\u26a0 {title}",  # warning triangle on the box title
                alert_box_style,
                f"\u26a0 {alert.get('flag', 'out_of_range').upper().replace('_', ' ')}: {describe_alert(alert)}",
            ]

    if not connected:
        validation_text = "Disconnected from telemetry broker"
    elif invalid_sample:
        validation_text = "WARNING - invalid or incomplete telemetry sample"
    elif not raw_fresh:
        validation_text = "STALE - waiting for live telemetry"
    elif status is not None and not status["online"]:
        validation_text = "Gateway offline"
    elif status is not None and (not gateway_fresh or not status["stm32_connected"] or not status["telemetry_fresh"]):
        validation_text = "STALE - STM32 link or gateway status unavailable"
    elif not processed_fresh:
        validation_text = "STALE - waiting for current data validation"
    elif active_alerts or current_errors:
        titles = {field: title for _, field, title in METRIC_BOXES}
        names = ", ".join(titles.get(f, f) for f in sorted(set(active_alerts) | current_errors))
        validation_text = f"\u26a0 WARNING - invalid or out-of-range data: {names}"
    elif not matching_validation:
        validation_text = "Waiting for validation of the displayed sample"
    else:
        validation_text = "\u2713 Data validation: all channels in range"
    healthy = validation_text.startswith("\u2713")
    pending = validation_text.startswith("Waiting for validation")
    validation_style = {
        "backgroundColor": "#142a23" if healthy else "#302717" if pending else "#321b24",
        "color": "#a7f3d0" if healthy else "#fde68a" if pending else "#fecdd3",
    }

    return (
        f"{latest['throttle_percent']:.2f} %",
        f"{latest['brake_bar']:.2f} bar",
        f"{latest['speed_kmh']:.2f} km/h",
        f"Latest packet: {latest['sequence']} · received {rate:.0f} Hz · sample age {age_text} · missing {missed} · ignored {rejected}",
        figure,
        *alert_outputs,
        validation_text,
        validation_style,
    )


@app.callback(
    Output("throttle-value", "children"),
    Output("brake-value", "children"),
    Output("speed-value", "children"),
    Output("sequence-value", "children"),
    # ADDED: per-box warning triangle, red highlight and warning text
    Output("throttle-title", "children"),
    Output("throttle-box", "style"),
    Output("throttle-warning", "children"),
    Output("brake-title", "children"),
    Output("brake-box", "style"),
    Output("brake-warning", "children"),
    Output("speed-title", "children"),
    Output("speed-box", "style"),
    Output("speed-warning", "children"),
    # ADDED: summary line
    Output("validation-status", "children"),
    Output("validation-status", "style"),
    Input("dashboard-update", "n_intervals"),
    running=[(Output("dashboard-update", "disabled"), True, False)],
)
def update_sensor_cards(n):
    result = update_dashboard(n, include_figure=False)
    return (*result[:4], *result[5:])


@app.callback(Output("telemetry-graphs", "figure"), Output("telemetry-graphs", "extendData"),
              Output("sensor-view-state", "data"), Input("sensor-update", "n_intervals"),
              Input("return-live", "n_clicks"), State("sensor-view-state", "data"),
              running=[(Output("sensor-update", "disabled"), True, False)])
def update_sensor_graph(_, live_revision, previous):
    source = hardware_source
    if source is not None:
        if not source.throttle_only:
            throttle, brake, revision, generation = source.pedal_history()
            current = {"revision": revision, "generation": generation, "live": live_revision}
            if previous == current:
                return no_update, no_update, no_update
            figure = make_live_pedal_figure(throttle, brake)
            figure.update_layout(uirevision=f"pedals:{generation}:{live_revision}")
            return figure, no_update, current
        arrays, revision, generation = source.sensor_history()
        millivolts = source.calibration is None
        ranges = [channel_range("throttle_percent", arrays[1], throttle_millivolts=millivolts)]
    else:
        with telemetry_lock:
            arrays = [list(h) for h in (time_history, throttle_history, brake_history, speed_history)]
            revision, generation = history_revision, history_generation
        ranges = [channel_range(field, values) for field, values in zip(DASHBOARD_FIELDS, arrays[1:])]
    current = {"revision": revision, "generation": generation, "live": live_revision, "ranges": ranges}
    added = revision - previous["revision"] if previous else 0
    if (not previous or previous["generation"] != generation or previous["live"] != live_revision
            or added < 0 or added > len(arrays[0])):
        figure = (make_throttle_bench_figure(*arrays, millivolts=millivolts)
                  if source is not None else make_sensor_figure(*arrays))
        figure.update_layout(uirevision=f"sensors:{generation}:{live_revision}")
        return figure, no_update, current
    figure = no_update
    if previous.get("ranges") != ranges:
        figure = Patch()
        for i, bounds in enumerate(ranges, 1):
            figure["layout"]["yaxis" + (str(i) if i > 1 else "")]["range"] = bounds
    if not added:
        return figure, no_update, current
    traces = list(range(len(ranges)))
    return figure, [{"x": [arrays[0][-added:]] * len(traces),
                     "y": [values[-added:] for values in arrays[1:]]}, traces, 1000], current


@app.callback(Output("lap-metrics", "children"), Output("lap-status", "children"),
              Output("track-trace", "figure"), Output("lap-delta-graph", "figure"),
              Output("lap-history", "children"), Output("lap-view-state", "data"),
              Output("lap-input-graphs", "figure"), Output("input-view-state", "data"),
              Output("reference-choice", "options"),
              Input("lap-update", "n_intervals"), Input("track-colour", "value"),
              Input("inspection", "data"), Input("return-live", "n_clicks"), State("lap-view-state", "data"),
              State("input-view-state", "data"), running=[(Output("lap-update", "disabled"), True, False)])
def update_lap_views(n, colour, inspection, live_revision, previous, previous_inputs):
    state = lap_snapshot()
    cards, status, track, delta, history = update_lap_dashboard(n, colour, inspection, live_revision, state)
    identity = [state["lap_key"], state["revision"], colour, live_revision, is_number(inspection)]
    track, track_meta = stream_figure(track, (previous or {}).get("track"), identity)
    delta, delta_meta = stream_figure(delta, (previous or {}).get("delta"), identity)
    inputs, inputs_meta = no_update, no_update
    if hardware_source is None:
        inputs, inputs_meta = stream_figure(lap_input_figure(state, inspection, live_revision).update_layout(**plot_style), previous_inputs,
                                           [state["lap_key"], state["revision"], live_revision])
    options = [{"label": "Session best (automatic)", "value": "best"}] + [
        {"label": f"Lap {number}", "value": number} for number in state.get("reference_choices", [])]
    return cards, status, track, delta, history, {"track": track_meta, "delta": delta_meta}, inputs, inputs_meta, options


@app.callback(Output("inspection", "data"), Input("lap-input-graphs", "hoverData"),
              Input("track-trace", "hoverData"), Input("lap-delta-graph", "hoverData"),
              Input("return-live", "n_clicks"), prevent_initial_call=True)
def inspect_position(inputs, track, delta, _):
    if ctx.triggered_id == "return-live":
        return None
    source = track if ctx.triggered_id == "track-trace" else delta if ctx.triggered_id == "lap-delta-graph" else inputs
    if source and source.get("points"):
        point = source["points"][0]
        distance = point.get("customdata") if ctx.triggered_id == "track-trace" else point.get("x")
        if is_number(distance):
            return distance
    return no_update


@app.callback(Output("reference-feedback", "children"), Input("reference-choice", "value"), prevent_initial_call=True)
def choose_reference(value):
    state = lap_snapshot()
    number = None if value == "best" else value
    if number is not None and (type(number) is not int or number not in state["reference_choices"]):
        return "That lap is no longer available in this session."
    if hardware_source is not None:
        try:
            hardware_source.select_reference(number)
        except ValueError:
            return "That lap is no longer available in this session."
        return "Session best selected" if number is None else f"Lap {number} selected"
    result = mqtt_client.publish(REFERENCE_TOPIC, json.dumps({"session_id": state["session_id"], "lap": number}), qos=1)
    return "Reference requested for this session" if result.rc == mqtt.MQTT_ERR_SUCCESS else "Lap service unavailable"


def main(argv=None):
    global hardware_source

    parser = argparse.ArgumentParser(description="Run the UMR telemetry dashboard.")
    parser.add_argument("--hardware-port", metavar="PORT",
                        help="STM32 USB port (COM5) or ESP32 TCP stream (socket://192.168.4.1:9000)")
    parser.add_argument("--throttle-bench", action="store_true",
                        help="Read the standalone STM32 throttle bench sketch")
    parser.add_argument("--throttle-calibration-mv", nargs=2, type=float, metavar=("RELEASED", "PRESSED"),
                        help="Show hardware throttle percentage using measured endpoint voltages in mV")
    parser.add_argument("--save-throttle-calibration", action="store_true",
                        help="Save the supplied throttle endpoints for later launches on this hardware port")
    parser.add_argument("--brake-calibration-v", nargs=2, type=float, metavar=("RELEASED", "FULL_SCALE"),
                        help="Set the brake percentage endpoints in volts (default: 0.490 and 4.500 V)")
    args = parser.parse_args(argv)
    if args.throttle_bench and not args.hardware_port:
        parser.error("--throttle-bench requires --hardware-port.")
    if args.throttle_calibration_mv is not None and not args.hardware_port:
        parser.error("--throttle-calibration-mv requires --hardware-port.")
    if args.save_throttle_calibration and args.throttle_calibration_mv is None:
        parser.error("--save-throttle-calibration requires --throttle-calibration-mv RELEASED PRESSED.")
    if args.brake_calibration_v is not None and (not args.hardware_port or args.throttle_bench):
        parser.error("--brake-calibration-v requires combined hardware mode.")
    if args.hardware_port:
        from hardware_bench import BrakeCalibration, SerialBench, ThrottleCalibration

        mode = "bench" if args.throttle_bench else "hardware"
        calibration_path = THROTTLE_CALIBRATION_DIR / f"{mode}-{quote(args.hardware_port, safe='')}.json"
        calibration = None
        try:
            brake_calibration = BrakeCalibration(*args.brake_calibration_v) if args.brake_calibration_v else None
            if args.throttle_calibration_mv is not None:
                calibration = ThrottleCalibration(*args.throttle_calibration_mv)
                if args.save_throttle_calibration:
                    calibration.save(calibration_path)
                    print(f"Saved throttle calibration to {calibration_path}")
        except (OSError, ValueError) as error:
            parser.error(str(error))
        if calibration is None:
            try:
                calibration = ThrottleCalibration.load(calibration_path)
            except (OSError, ValueError) as error:
                print(f"Could not read saved throttle calibration at {calibration_path}: {error}")
            if calibration is not None:
                print(f"Loaded throttle calibration: {calibration.released_mv:g} mV released, "
                      f"{calibration.pressed_mv:g} mV pressed")
        hardware_source = SerialBench(args.hardware_port, calibration, throttle_only=args.throttle_bench,
                                      brake_calibration=brake_calibration)
        reader = threading.Thread(target=hardware_source.read, name="hardware-telemetry", daemon=True)
        reader.start()
        app.layout["lap-input-graphs"].style = {"display": "none"}
        print(f"Starting hardware telemetry at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
        try:
            app.run(debug=False, host=DASHBOARD_HOST, port=DASHBOARD_PORT)
        finally:
            hardware_source.stop.set()
            reader.join()
            hardware_source = None
        return

    print(f"Starting dashboard at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    start_mqtt(mqtt_client, BROKER, PORT)
    try:
        app.run(debug=False, host=DASHBOARD_HOST, port=DASHBOARD_PORT)
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()


if __name__ == "__main__":
    main()

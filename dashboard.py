import threading
import time
import json
from collections import deque

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
from telemetry_views import lap_input_figure, stream_figure
from lap_stream import RemoteLapAnalysis, STATE_TOPIC, UPDATE_TOPIC, REQUEST_TOPIC, REFERENCE_TOPIC


TOPIC = "umf1/car01/telemetry/fast"

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


app = Dash(__name__)

box_style = {
    "color": "#172033",
    "width": "200px",
    "padding": "20px",
    "margin": "10px",
    "backgroundColor": "#eeeeee",
    "borderRadius": "10px",
    "textAlign": "center",
}

# ADDED: (component id prefix, wire field name, title shown on the box)
METRIC_BOXES = [
    ("throttle", "throttle_percent", "Throttle"),
    ("brake", "brake_bar", "Brake Pressure"),
    ("steering", "steering_degrees", "Steering Angle"),
    ("speed", "speed_kmh", "Vehicle Speed"),
]

# ADDED: look of a box that currently has an out-of-range warning.
# boxShadow (not border) so the box does not change size or jump.
alert_box_style = {
    **box_style,
    "backgroundColor": "#f8d7da",
    "boxShadow": "0 0 0 3px #dc3545",
}

warning_text_style = {
    "fontSize": "13px",
    "fontWeight": "bold",
    "color": "#842029",
    "minHeight": "36px",
    "margin": "6px 0 0 0",
}

validation_base_style = {
    "padding": "14px",
    "margin": "10px 10px 20px 10px",
    "borderRadius": "10px",
    "textAlign": "center",
    "fontWeight": "bold",
    "maxWidth": "760px",
}


def make_metric_box(key, title):
    return html.Div(
        [
            html.H3(title, id=f"{key}-title"),
            html.H2(id=f"{key}-value"),
            html.P(id=f"{key}-warning", style=warning_text_style),
        ],
        id=f"{key}-box",
        style=box_style,
    )


app.layout = html.Div(
    [
        html.P("UM RACING  /  CAR 01", style={"letterSpacing": "3px", "color": "#94a3b8"}),
        html.H1("Pit Wall · Live telemetry"),
        html.Div([
            html.Div([html.Small("LAP TIME · LIVE"),
                      html.Div("—", id="lap-live-clock", **{"data-lap-clock": "true"},
                               style={"fontSize": "36px", "fontWeight": "bold", "fontVariantNumeric": "tabular-nums"})],
                     style={"padding": "20px", "background": "#172033", "borderRadius": "12px", "flex": "1 1 180px"}),
            html.Div(id="lap-metrics", style={"display": "contents"}),
        ], style={"display": "flex", "flexWrap": "wrap", "gap": "12px"}),
        dcc.Store(id="lap-clock-state"),
        dcc.Store(id="lap-clock-ack"),
        html.P(id="lap-status", style={"color": "#94a3b8"}),
        html.Div([
            html.Label(["Reference lap", dcc.Dropdown(id="reference-choice", value="best", clearable=False,
                        options=[{"label": "Session best (automatic)", "value": "best"}], style={"color": "#172033", "width": "260px"})]),
            html.Label(["Track colour", dcc.Dropdown(id="track-colour", value="inputs", clearable=False,
                        options=[{"label": label, "value": value} for label, value in
                                 (("Pedal activity", "inputs"), ("Speed", "speed_kmh"), ("Throttle", "throttle_percent"), ("Brake pressure", "brake_bar"))],
                        style={"color": "#172033", "width": "220px"})]),
            html.Button("Return to live", id="return-live", n_clicks=0),
            html.Span(id="reference-feedback"),
        ], style={"display": "flex", "gap": "16px", "alignItems": "end", "flexWrap": "wrap"}),
        dcc.Store(id="inspection"), dcc.Store(id="input-view-state"),
        dcc.Store(id="sensor-view-state"), dcc.Store(id="lap-view-state"),
        html.Div([
            dcc.Graph(id="track-trace", style={"flex": "2 1 480px"}),
            html.Div([html.H3("Session laps"), html.Div(id="lap-history")],
                     style={"flex": "1 1 240px", "padding": "20px", "background": "#172033", "borderRadius": "12px"}),
        ], style={"display": "flex", "flexWrap": "wrap", "gap": "16px"}),
        dcc.Graph(id="lap-delta-graph"),
        dcc.Graph(id="lap-input-graphs"),

        # CHANGED: the four boxes are now built by make_metric_box(), which
        # adds a title id, a box id and a reserved warning line to each.
        # The value ids (throttle-value, ...) are exactly as before.
        html.Div(
            [make_metric_box(key, title) for key, _, title in METRIC_BOXES],
            style={
                "display": "flex",
                "flexWrap": "wrap",
            },
        ),

        # ADDED: one-line summary of the validation status
        html.Div(id="validation-status"),

        html.P(id="sequence-value"),

        dcc.Graph(id="telemetry-graphs"),

        dcc.Interval(
            id="dashboard-update",
            interval=200,
            n_intervals=0,
        ),
        dcc.Interval(id="lap-update", interval=200, n_intervals=0),
        dcc.Interval(id="clock-update", interval=100, n_intervals=0),
        dcc.Interval(id="sensor-update", interval=100, n_intervals=0),
    ],
    style={
        "fontFamily": "Arial",
        "padding": "30px",
        "backgroundColor": "#0b1120",
        "color": "#e2e8f0",
        "minHeight": "100vh",
    },
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
    colors = {"throttle": ("Throttle", "#22c55e"), "brake": ("Braking", "#ef4444"),
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
        indices = [i for i, sample in enumerate(channels) if i < len(current) and is_number(sample.get(colour))]
        figure.add_trace(go.Scatter(x=[current[i][0] for i in indices], y=[current[i][1] for i in indices],
                                   customdata=[distances[i] if i < len(distances) else None for i in indices],
                                   mode="markers", name=colour.replace("_", " "),
                                   marker={"size": 7, "color": [channels[i][colour] for i in indices],
                                           "colorscale": "Turbo", "showscale": True,
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
    figure.update_layout(template="plotly_dark", paper_bgcolor="#0b1120", plot_bgcolor="#172033",
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
    delta.update_layout(template="plotly_dark", paper_bgcolor="#0b1120", plot_bgcolor="#172033",
                        title="Live delta vs reference lap · negative is faster", height=230,
                        uirevision=f"delta:{live_revision}", xaxis_title="Distance along reference lap (m)", yaxis_title="Delta (s)")
    return figure, delta


def update_lap_dashboard(_, colour="inputs", inspection=None, live_revision=0, state=None):
    if state is None:
        with telemetry_lock:
            state = lap_analysis.snapshot()
    delta = state["delta"]
    metrics = [("CURRENT LAP", str(state["lap_number"] or "—")),
               ("LAST LAP", lap_time(state["last"])),
               ("BEST LAP", lap_time(state["best"])),
               ("LIVE DELTA", "—" if delta is None else f"{delta:+.3f} s"),
               ("SESSION TOP SPEED", speed_text(state["session_top_speed"]) + " km/h")]
    cards = [html.Div([html.Small(label, style={"color": "#94a3b8", "letterSpacing": "1px"}),
                      html.Div(value, style={"fontSize": "30px", "fontWeight": "bold",
                                            "color": ("#34d399" if delta <= 0 else "#fb7185") if label == "LIVE DELTA" and delta is not None else "#f8fafc"})],
                     style={"flex": "1 1 160px", "padding": "20px", "background": "#172033", "borderRadius": "12px"})
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
    with telemetry_lock:
        return lap_analysis.clock_state()


app.clientside_callback(ClientsideFunction(namespace="telemetryClock", function_name="update"),
                        Output("lap-clock-ack", "data"), Input("lap-clock-state", "data"))


def speed_text(value):
    return "—" if value is None else f"{value:.0f}"


def lap_table(state):
    cell = {"padding": "7px 8px", "borderBottom": "1px solid #293140"}
    def row(number, seconds, speed, difference, best=False, live=False):
        label = "BEST" if best else "—" if difference is None else f"{difference:+.3f}"
        color = "#4ade80" if best or (difference is not None and difference <= .1) else "#fbbf24" if difference is not None and difference < 1 else "#fb7185"
        return html.Tr([
            html.Td(number, style={**cell, "color": "#94a3b8"}),
            html.Td(lap_time(seconds), style=cell, **({"data-lap-clock": "true"} if live else {})),
            html.Td(speed_text(speed), style={**cell, "color": "#94a3b8"}),
            html.Td(label, style={**cell, "color": color, "textAlign": "right"}),
        ], style={"background": "#20352d" if best else "#253044" if live else "transparent"})
    rows = [row("SESSION BEST", state["best"], state["best_top_speed"], None, best=state["best"] is not None)]
    for lap in state["history"]:
        # Recompute against the CURRENT session best, so every row uses
        # one consistent reference when a new fastest lap is completed.
        rows.append(row(str(lap["lap_number"]), lap["lap_time_seconds"], lap["top_speed_kmh"],
                        lap["lap_time_seconds"] - state["best"], lap["lap_number"] == state.get("best_lap", state["reference_lap"])))
    if state["lap_number"] is not None:
        rows.append(row(f"{state['lap_number']} LIVE", state["elapsed"], state["lap_top_speed"], state["delta"], live=True))
    return html.Div([
        html.Div([html.Strong("Whole stint"), html.Span(f"{state['completed_count']} laps", style={"float": "right", "color": "#94a3b8"})]),
        html.P("GPS top speed · completed-lap delta vs session best", style={"fontSize": "11px", "color": "#94a3b8"}),
        html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label, style={**cell, "color": "#94a3b8", "fontSize": "11px"}) for label in ("#", "TIME", "TOP KPH", "DELTA")])),
            html.Tbody(rows),
        ], style={"width": "100%", "borderCollapse": "collapse", "fontFamily": "monospace", "fontVariantNumeric": "tabular-nums"}),
                 style={"maxHeight": "420px", "overflowY": "auto"}),
    ])


def channel_range(field, values):
    spec = CHANNEL_SPECS[field]
    finite = [v for v in values if is_number(v)]
    low = min([spec["min_value"]] + finite)
    high = max([spec["max_value"]] + finite)
    return [low, high if high > low else low + 1]


def make_sensor_figure(graph_times, graph_throttle, graph_brake, graph_steering, graph_speed):
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.07,
        subplot_titles=(
            "Throttle Position",
            "Brake Pressure",
            "Steering Angle",
            "Vehicle Speed",
        ),
    )

    figure.add_trace(
        go.Scatter(
            x=graph_times,
            y=graph_throttle,
            mode="lines",
            name="Throttle",
            line={"color": "#22c55e"},
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
            y=graph_steering,
            mode="lines",
            name="Steering",
            line={"color": "#fbbf24"},
        ),
        row=3,
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
        row=4,
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
        title_text="Steering (°)",
        range=channel_range("steering_degrees", graph_steering),
        row=3,
        col=1,
    )

    figure.update_yaxes(
        title_text="Speed (km/h)",
        range=channel_range("speed_kmh", graph_speed),
        row=4,
        col=1,
    )

    figure.update_xaxes(
        title_text="Time (seconds)",
        row=4,
        col=1,
    )

    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0b1120",
        plot_bgcolor="#172033",
        height=1000,
        showlegend=False,
        title="Live Telemetry History",
        uirevision="sensors",
    )

    return figure


def update_dashboard(_, include_figure=True):
    with telemetry_lock:
        latest = telemetry.copy()

        graph_times = list(time_history)
        graph_throttle = list(throttle_history)
        graph_brake = list(brake_history)
        graph_steering = list(steering_history)
        graph_speed = list(speed_history)

        # ADDED: snapshot of warnings that are still active
        active_alerts = alert_tracker.active()
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
        current_errors = {field for field, flag in alert_tracker.latest_flags.items() if flag != "ok"}
        connected = mqtt_connected
        invalid_sample = raw_invalid
        status = gateway_status.copy() if gateway_status else None
        gateway_fresh = last_gateway_time is not None and now - last_gateway_time <= GATEWAY_FRESHNESS_SECONDS

    figure = make_sensor_figure(graph_times, graph_throttle, graph_brake, graph_steering, graph_speed) if include_figure else None

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
        **validation_base_style,
        "backgroundColor": "#d1e7dd" if healthy else "#fef3c7" if pending else "#f8d7da",
        "color": "#0f5132" if healthy else "#92400e" if pending else "#842029",
    }

    return (
        f"{latest['throttle_percent']:.2f} %",
        f"{latest['brake_bar']:.2f} bar",
        f"{latest['steering_degrees']:.2f}°",
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
    Output("steering-value", "children"),
    Output("speed-value", "children"),
    Output("sequence-value", "children"),
    # ADDED: per-box warning triangle, red highlight and warning text
    Output("throttle-title", "children"),
    Output("throttle-box", "style"),
    Output("throttle-warning", "children"),
    Output("brake-title", "children"),
    Output("brake-box", "style"),
    Output("brake-warning", "children"),
    Output("steering-title", "children"),
    Output("steering-box", "style"),
    Output("steering-warning", "children"),
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
    return (*result[:5], *result[6:])


@app.callback(Output("telemetry-graphs", "figure"), Output("telemetry-graphs", "extendData"),
              Output("sensor-view-state", "data"), Input("sensor-update", "n_intervals"),
              Input("return-live", "n_clicks"), State("sensor-view-state", "data"),
              running=[(Output("sensor-update", "disabled"), True, False)])
def update_sensor_graph(_, live_revision, previous):
    with telemetry_lock:
        arrays = [list(h) for h in (time_history, throttle_history, brake_history, steering_history, speed_history)]
        revision, generation = history_revision, history_generation
    ranges = [channel_range(field, values) for field, values in zip(FAST_FIELDS, arrays[1:])]
    current = {"revision": revision, "generation": generation, "live": live_revision, "ranges": ranges}
    added = revision - previous["revision"] if previous else 0
    if (not previous or previous["generation"] != generation or previous["live"] != live_revision
            or added < 0 or added > len(arrays[0])):
        figure = make_sensor_figure(*arrays)
        figure.update_layout(uirevision=f"sensors:{generation}:{live_revision}")
        return figure, no_update, current
    figure = no_update
    if previous.get("ranges") != ranges:
        figure = Patch()
        for i, bounds in enumerate(ranges, 1):
            figure["layout"]["yaxis" + (str(i) if i > 1 else "")]["range"] = bounds
    if not added:
        return figure, no_update, current
    return figure, [{"x": [arrays[0][-added:]] * 4, "y": [values[-added:] for values in arrays[1:]]}, [0, 1, 2, 3], 1000], current


@app.callback(Output("lap-metrics", "children"), Output("lap-status", "children"),
              Output("track-trace", "figure"), Output("lap-delta-graph", "figure"),
              Output("lap-history", "children"), Output("lap-view-state", "data"),
              Output("lap-input-graphs", "figure"), Output("input-view-state", "data"),
              Output("reference-choice", "options"),
              Input("lap-update", "n_intervals"), Input("track-colour", "value"),
              Input("inspection", "data"), Input("return-live", "n_clicks"), State("lap-view-state", "data"),
              State("input-view-state", "data"), running=[(Output("lap-update", "disabled"), True, False)])
def update_lap_views(n, colour, inspection, live_revision, previous, previous_inputs):
    with telemetry_lock:
        state = lap_analysis.snapshot()
    cards, status, track, delta, history = update_lap_dashboard(n, colour, inspection, live_revision, state)
    identity = [state["lap_key"], state["revision"], colour, live_revision, is_number(inspection)]
    track, track_meta = stream_figure(track, (previous or {}).get("track"), identity)
    delta, delta_meta = stream_figure(delta, (previous or {}).get("delta"), identity)
    inputs, inputs_meta = stream_figure(lap_input_figure(state, inspection, live_revision), previous_inputs,
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
    with telemetry_lock:
        state = lap_analysis.snapshot()
    number = None if value == "best" else value
    if number is not None and (type(number) is not int or number not in state["reference_choices"]):
        return "That lap is no longer available in this session."
    result = mqtt_client.publish(REFERENCE_TOPIC, json.dumps({"session_id": state["session_id"], "lap": number}), qos=1)
    return "Reference requested for this session" if result.rc == mqtt.MQTT_ERR_SUCCESS else "Lap service unavailable"


if __name__ == "__main__":
    print(f"Starting dashboard at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    start_mqtt(mqtt_client, BROKER, PORT)
    try:
        app.run(debug=False, host=DASHBOARD_HOST, port=DASHBOARD_PORT)
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()

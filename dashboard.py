import threading
import time
from collections import deque

import paho.mqtt.client as mqtt
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html
from plotly.subplots import make_subplots

# ADDED: keeps out-of-range warnings visible for a few seconds
from flag_alerts import AlertTracker, describe_alert
from mqtt_runtime import start_mqtt
from lap_analysis import LapAnalysis
from lap_timer import GPS_TOPIC
from telemetry_validation import FAST_FIELDS, decode_object, is_number, validate_fast_sample


BROKER = "localhost"
PORT = 1883
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

# ADDED: remembers which channels were recently flagged out of range
alert_tracker = AlertTracker()
lap_analysis = LapAnalysis()


def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    with telemetry_lock:
        mqtt_connected = reason_code == 0
    if reason_code == 0:
        client.subscribe(TOPIC)
        client.subscribe(PROCESSED_TOPIC)
        client.subscribe(GATEWAY_STATUS_TOPIC, qos=1)
        client.subscribe(GPS_TOPIC)


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
        lap_analysis.update(gps)


def on_message(client, userdata, message):
    global last_raw_time, raw_invalid
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
        telemetry.clear()
        telemetry.update(received)
        for history, value in zip((time_history, throttle_history, brake_history,
                                   steering_history, speed_history), sample):
            history.append(value)
        last_raw_time = time.monotonic()
        raw_invalid = False


def on_processed_message(client, userdata, message):
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
            alert_tracker.update(processed)
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
        html.Div(id="lap-metrics", style={"display": "flex", "flexWrap": "wrap", "gap": "12px"}),
        html.P(id="lap-status", style={"color": "#94a3b8"}),
        html.Div([
            dcc.Graph(id="track-trace", style={"flex": "2 1 480px"}),
            html.Div([html.H3("Completed laps"), html.Div(id="lap-history")],
                     style={"flex": "1 1 240px", "padding": "20px", "background": "#172033", "borderRadius": "12px"}),
        ], style={"display": "flex", "flexWrap": "wrap", "gap": "16px"}),
        dcc.Graph(id="lap-delta-graph"),

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
            interval=100,
            n_intervals=0,
        ),
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


def lap_figures(state):
    figure = go.Figure()
    reference, current = state["reference"], state["current"]
    if reference:
        figure.add_trace(go.Scatter(x=[p[0] for p in reference], y=[p[1] for p in reference],
                                   mode="lines", name="Best lap", line={"color": "#64748b", "width": 5}))
    if current:
        figure.add_trace(go.Scatter(x=[p[0] for p in current], y=[p[1] for p in current],
                                   mode="lines", name="Current trace", line={"color": "#38bdf8", "width": 3}))
        figure.add_trace(go.Scatter(x=[current[-1][0]], y=[current[-1][1]], mode="markers",
                                   name="Car" if state["fresh"] else "Last position",
                                   marker={"color": "#34d399" if state["fresh"] else "#94a3b8", "size": 12}))
    figure.update_layout(template="plotly_dark", paper_bgcolor="#0b1120", plot_bgcolor="#172033",
                         title="GPS track trace", height=420, uirevision="track",
                         xaxis_title="East (m)", yaxis_title="North (m)",
                         yaxis={"scaleanchor": "x", "scaleratio": 1}, margin={"t": 55, "b": 45, "l": 55, "r": 20})
    if not current and not reference:
        figure.add_annotation(text="Waiting for a valid GPS fix", xref="paper", yref="paper", x=.5, y=.5, showarrow=False)
    delta = go.Figure(go.Scatter(x=[p[0] for p in state["delta_trace"]],
                                y=[p[1] for p in state["delta_trace"]], mode="lines",
                                line={"color": "#38bdf8"}, name="Delta"))
    delta.add_hline(y=0, line_color="#64748b")
    delta.update_layout(template="plotly_dark", paper_bgcolor="#0b1120", plot_bgcolor="#172033",
                        title="Live delta vs best lap · negative is faster", height=230,
                        xaxis_title="Distance along reference lap (m)", yaxis_title="Delta (s)")
    return figure, delta


@app.callback(Output("lap-metrics", "children"), Output("lap-status", "children"),
              Output("track-trace", "figure"), Output("lap-delta-graph", "figure"),
              Output("lap-history", "children"), Input("dashboard-update", "n_intervals"))
def update_lap_dashboard(_):
    with telemetry_lock:
        state = lap_analysis.snapshot()
    delta = state["delta"]
    metrics = [("CURRENT LAP", str(state["lap_number"] or "—")),
               ("ELAPSED", lap_time(state["elapsed"])), ("LAST LAP", lap_time(state["last"])),
               ("BEST LAP", lap_time(state["best"])),
               ("LIVE DELTA", "—" if delta is None else f"{delta:+.3f} s")]
    cards = [html.Div([html.Small(label, style={"color": "#94a3b8", "letterSpacing": "1px"}),
                      html.Div(value, style={"fontSize": "30px", "fontWeight": "bold",
                                            "color": ("#34d399" if delta <= 0 else "#fb7185") if label == "LIVE DELTA" and delta is not None else "#f8fafc"})],
                     style={"flex": "1 1 160px", "padding": "20px", "background": "#172033", "borderRadius": "12px"})
             for label, value in metrics]
    if not state["fresh"]:
        status = "GPS unavailable or stale · lap timing paused"
    elif state["elapsed"] is None:
        status = "Waiting for the next finish-line crossing to start a complete lap"
    elif not state["reference"]:
        status = "Recording the first complete lap · delta becomes available after completion"
    elif delta is None:
        status = "Position cannot be matched reliably to the reference · delta unavailable"
    else:
        status = f"Comparing with best lap {state['reference_lap']} · negative delta means faster"
    rows = [html.Tr([html.Th("Lap"), html.Th("Time"), html.Th("vs prior best")])]
    for lap in reversed(state["history"]):
        rows.append(html.Tr([html.Td(str(lap["lap_number"])), html.Td(lap_time(lap["lap_time_seconds"])),
                             html.Td("—" if lap["delta"] is None else f"{lap['delta']:+.3f} s")]))
    history = html.Table(rows, style={"width": "100%", "textAlign": "left", "lineHeight": "2.2"}) if state["history"] else html.P("Complete a timed lap to see results here.")
    track, delta_figure = lap_figures(state)
    return cards, status, track, delta_figure, history


@app.callback(
    Output("throttle-value", "children"),
    Output("brake-value", "children"),
    Output("steering-value", "children"),
    Output("speed-value", "children"),
    Output("sequence-value", "children"),
    Output("telemetry-graphs", "figure"),
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
)
def update_dashboard(_):
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
        raw_fresh = last_raw_time is not None and now - last_raw_time <= FRESHNESS_SECONDS
        processed_fresh = (alert_tracker.last_processed_time is not None
                           and now - alert_tracker.last_processed_time <= FRESHNESS_SECONDS)
        current_errors = {field for field, flag in alert_tracker.latest_flags.items() if flag != "ok"}
        connected = mqtt_connected
        invalid_sample = raw_invalid
        status = gateway_status.copy() if gateway_status else None
        gateway_fresh = last_gateway_time is not None and now - last_gateway_time <= GATEWAY_FRESHNESS_SECONDS

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
        ),
        row=4,
        col=1,
    )

    figure.update_yaxes(
        title_text="Throttle (%)",
        range=[0, 100],
        row=1,
        col=1,
    )

    figure.update_yaxes(
        title_text="Brake (bar)",
        range=[0, 100],
        row=2,
        col=1,
    )

    figure.update_yaxes(
        title_text="Steering (°)",
        range=[-50, 50],
        row=3,
        col=1,
    )

    figure.update_yaxes(
        title_text="Speed (km/h)",
        range=[0, 100],
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
    )

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
    else:
        validation_text = "\u2713 Data validation: all channels in range"
    healthy = validation_text.startswith("\u2713")
    validation_style = {
        **validation_base_style,
        "backgroundColor": "#d1e7dd" if healthy else "#f8d7da",
        "color": "#0f5132" if healthy else "#842029",
    }

    return (
        f"{latest['throttle_percent']:.2f} %",
        f"{latest['brake_bar']:.2f} bar",
        f"{latest['steering_degrees']:.2f}°",
        f"{latest['speed_kmh']:.2f} km/h",
        f"Latest packet: {latest['sequence']}",
        figure,
        *alert_outputs,
        validation_text,
        validation_style,
    )


if __name__ == "__main__":
    print("Starting dashboard at http://127.0.0.1:8050")
    start_mqtt(mqtt_client, BROKER, PORT)
    try:
        app.run(debug=False, host="127.0.0.1", port=8050)
    finally:
        mqtt_client.disconnect()
        mqtt_client.loop_stop()

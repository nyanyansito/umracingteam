import json
import threading
from collections import deque

import paho.mqtt.client as mqtt
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html
from plotly.subplots import make_subplots

# ADDED: keeps out-of-range warnings visible for a few seconds
from flag_alerts import AlertTracker, describe_alert


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

time_history = deque(maxlen=100)
throttle_history = deque(maxlen=100)
brake_history = deque(maxlen=100)
steering_history = deque(maxlen=100)
speed_history = deque(maxlen=100)

# ADDED: remembers which channels were recently flagged out of range
alert_tracker = AlertTracker()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Dashboard connected to Mosquitto")
        client.subscribe(TOPIC)
        client.subscribe(PROCESSED_TOPIC)  # ADDED
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    try:
        received_data = json.loads(
            message.payload.decode("utf-8")
        )

        with telemetry_lock:
            telemetry.update(received_data)

            time_history.append(
                received_data["timestamp_ms"] / 1000
            )
            throttle_history.append(
                received_data["throttle_percent"]
            )
            brake_history.append(
                received_data["brake_bar"]
            )
            steering_history.append(
                received_data["steering_degrees"]
            )
            speed_history.append(
                received_data["speed_kmh"]
            )

    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        print("Dashboard received an invalid message")


# ADDED: handler used ONLY for PROCESSED_TOPIC (see message_callback_add
# below), so on_message above is untouched and still handles TOPIC.
def on_processed_message(client, userdata, message):
    try:
        processed_message = json.loads(
            message.payload.decode("utf-8")
        )

        with telemetry_lock:
            alert_tracker.update(processed_message)

    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Dashboard received an invalid processed message")


mqtt_client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-dashboard"
)

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.message_callback_add(PROCESSED_TOPIC, on_processed_message)  # ADDED
mqtt_client.connect(BROKER, PORT)
mqtt_client.loop_start()


app = Dash(__name__)

box_style = {
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
        html.H1("UM F1 Pit-Wall Telemetry"),

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
    },
)


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
        processed_seen = alert_tracker.processed_seen

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
                f"\u26a0 OUT OF RANGE: {describe_alert(alert)}",
            ]

    # ADDED: one-line summary under the boxes
    if active_alerts:
        titles = {field: title for _, field, title in METRIC_BOXES}
        names = ", ".join(titles.get(f, f) for f in active_alerts)
        validation_text = f"\u26a0 WARNING - out-of-range data: {names}"
        validation_style = {
            **validation_base_style,
            "backgroundColor": "#f8d7da",
            "color": "#842029",
        }
    elif processed_seen:
        validation_text = "\u2713 Data validation: all channels in range"
        validation_style = {
            **validation_base_style,
            "backgroundColor": "#d1e7dd",
            "color": "#0f5132",
        }
    else:
        validation_text = (
            "Data validation: waiting for data_processing.py "
            "(is it running?)"
        )
        validation_style = {
            **validation_base_style,
            "backgroundColor": "#e0e0e0",
            "color": "#555555",
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
    app.run(debug=False, host="127.0.0.1", port=8050)
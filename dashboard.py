import json
import threading
from collections import deque

import paho.mqtt.client as mqtt
import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html
from plotly.subplots import make_subplots


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/fast"

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


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Dashboard connected to Mosquitto")
        client.subscribe(TOPIC)
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


mqtt_client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-dashboard"
)

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
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

app.layout = html.Div(
    [
        html.H1("UM F1 Pit-Wall Telemetry"),

        html.Div(
            [
                html.Div(
                    [
                        html.H3("Throttle"),
                        html.H2(id="throttle-value"),
                    ],
                    style=box_style,
                ),

                html.Div(
                    [
                        html.H3("Brake Pressure"),
                        html.H2(id="brake-value"),
                    ],
                    style=box_style,
                ),

                html.Div(
                    [
                        html.H3("Steering Angle"),
                        html.H2(id="steering-value"),
                    ],
                    style=box_style,
                ),

                html.Div(
                    [
                        html.H3("Vehicle Speed"),
                        html.H2(id="speed-value"),
                    ],
                    style=box_style,
                ),
            ],
            style={
                "display": "flex",
                "flexWrap": "wrap",
            },
        ),

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

    return (
        f"{latest['throttle_percent']:.2f} %",
        f"{latest['brake_bar']:.2f} bar",
        f"{latest['steering_degrees']:.2f}°",
        f"{latest['speed_kmh']:.2f} km/h",
        f"Latest packet: {latest['sequence']}",
        figure,
    )


if __name__ == "__main__":
    print("Starting dashboard at http://127.0.0.1:8050")
    app.run(debug=False, host="127.0.0.1", port=8050)
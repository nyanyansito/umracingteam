import json
import threading

import paho.mqtt.client as mqtt
from dash import Dash, Input, Output, dcc, html


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/fast"

telemetry = {
    "sequence": 0,
    "throttle_percent": 0,
    "brake_bar": 0,
    "steering_degrees": 0,
    "speed_kmh": 0,
}

telemetry_lock = threading.Lock()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Dashboard connected to Mosquitto")
        client.subscribe(TOPIC)
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    try:
        received_data = json.loads(message.payload.decode("utf-8"))

        with telemetry_lock:
            telemetry.update(received_data)

    except (json.JSONDecodeError, UnicodeDecodeError):
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
    Input("dashboard-update", "n_intervals"),
)
def update_dashboard(_):
    with telemetry_lock:
        latest = telemetry.copy()

    return (
        f"{latest['throttle_percent']:.2f} %",
        f"{latest['brake_bar']:.2f} bar",
        f"{latest['steering_degrees']:.2f}°",
        f"{latest['speed_kmh']:.2f} km/h",
        f"Latest packet: {latest['sequence']}",
    )


if __name__ == "__main__":
    print("Starting dashboard at http://127.0.0.1:8050")
    app.run(debug=False, host="127.0.0.1", port=8050)
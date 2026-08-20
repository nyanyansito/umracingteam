import json

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/#"


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to Mosquitto")
        print(f"Subscribed to {TOPIC}")
        client.subscribe(TOPIC)
    else:
        print(f"Connection failed: {reason_code}")


def on_message(client, userdata, message):
    text = message.payload.decode("utf-8")

    print()
    print(f"Topic: {message.topic}")

    try:
        data = json.loads(text)
        print(f"Data: {data}")
    except json.JSONDecodeError:
        print(f"Message: {text}")


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-python-subscriber"
)

client.on_connect = on_connect
client.on_message = on_message

client.connect(BROKER, PORT)
client.loop_forever()
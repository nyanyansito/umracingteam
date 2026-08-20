import json

import paho.mqtt.client as mqtt


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/status"


message = {
    "online": True,
    "source": "python"
}

client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-python-publisher"
)

client.connect(BROKER, PORT)
client.loop_start()

result = client.publish(TOPIC, json.dumps(message))
result.wait_for_publish()

print(f"Published to {TOPIC}")
print(message)

client.loop_stop()
client.disconnect()
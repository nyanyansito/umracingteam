import json

import paho.mqtt.client as mqtt

from sequence_tracker import SequenceTracker


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/#"

sequence_trackers = {}
total_missed_packets = 0
total_duplicate_packets = 0


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to Mosquitto")
        print(f"Subscribed to {TOPIC}")
        client.subscribe(TOPIC)
    else:
        print(f"Connection failed: {reason_code}")


def check_sequence(topic, data):
    global total_missed_packets
    global total_duplicate_packets

    if "sequence" not in data:
        return

    if topic not in sequence_trackers:
        sequence_trackers[topic] = SequenceTracker()

    result = sequence_trackers[topic].check(
        data["sequence"]
    )

    if result["status"] == "gap":
        missed = result["missed_packets"]
        total_missed_packets += missed

        print(
            f"WARNING: Missed {missed} packet(s). "
            f"Expected {result['expected_sequence']}, "
            f"received {result['received_sequence']}."
        )

        print(
            f"Total missed packets: "
            f"{total_missed_packets}"
        )

    elif result["status"] == "duplicate":
        total_duplicate_packets += 1

        print(
            f"WARNING: Duplicate packet "
            f"{data['sequence']}."
        )

        print(
            f"Total duplicate packets: "
            f"{total_duplicate_packets}"
        )


def on_message(client, userdata, message):
    text = message.payload.decode("utf-8")

    print()
    print(f"Topic: {message.topic}")

    try:
        data = json.loads(text)

        check_sequence(message.topic, data)

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

try:
    client.loop_forever()

except KeyboardInterrupt:
    print()
    print("Subscriber stopped")

finally:
    client.disconnect()
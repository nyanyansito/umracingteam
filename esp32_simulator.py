import json
import time

import paho.mqtt.client as mqtt

from stm32_simulator import generate_telemetry
from telemetry_packet import (
    decode_fast_telemetry,
    encode_fast_telemetry,
)


BROKER = "localhost"
PORT = 1883
TOPIC = "umf1/car01/telemetry/fast"

UPDATE_RATE_HZ = 10
UPDATE_INTERVAL_SECONDS = 1 / UPDATE_RATE_HZ


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-virtual-esp32"
)

print("Virtual ESP32 connecting to Mosquitto...")

client.connect(BROKER, PORT)
client.loop_start()

print("Virtual ESP32 connected")
print("Receiving simulated binary UART packets")
print("Press Control + C to stop")

start_time = time.monotonic()
sequence = 0

try:
    while True:
        elapsed = time.monotonic() - start_time

        stm32_telemetry = generate_telemetry(sequence, elapsed)

        # Simulate the binary packet sent through UART.
        uart_packet = encode_fast_telemetry(stm32_telemetry)

        # Simulate the ESP32 validating and decoding the UART packet.
        decoded_telemetry = decode_fast_telemetry(uart_packet)

        mqtt_message = json.dumps(decoded_telemetry)
        client.publish(TOPIC, mqtt_message)

        print(
            f"Packet {sequence}: "
            f"UART={len(uart_packet)} bytes, "
            f"throttle={decoded_telemetry['throttle_percent']}%, "
            f"brake={decoded_telemetry['brake_bar']} bar, "
            f"speed={decoded_telemetry['speed_kmh']} km/h"
        )

        sequence = (sequence + 1) % 65536
        time.sleep(UPDATE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print()
    print("Virtual ESP32 stopped")

finally:
    client.loop_stop()
    client.disconnect()
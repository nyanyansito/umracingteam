import json
import time

import paho.mqtt.client as mqtt

from stm32_simulator import generate_telemetry


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
print("Publishing simulated STM32 data")
print("Press Control + C to stop")

start_time = time.monotonic()
sequence = 0

try:
    while True:
        elapsed = time.monotonic() - start_time

        # Pretend this data was received from the STM32 through UART.
        telemetry = generate_telemetry(sequence, elapsed)

        message = json.dumps(telemetry)
        client.publish(TOPIC, message)

        print(
            f"Published packet {sequence}: "
            f"throttle={telemetry['throttle_percent']}%, "
            f"brake={telemetry['brake_bar']} bar, "
            f"speed={telemetry['speed_kmh']} km/h"
        )

        sequence += 1
        time.sleep(UPDATE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print()
    print("Virtual ESP32 stopped")

finally:
    client.loop_stop()
    client.disconnect()
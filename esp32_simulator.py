import json
import socket
import struct
import time

import paho.mqtt.client as mqtt

from telemetry_packet import (
    CRC_SIZE,
    HEADER_SIZE,
    PAYLOAD_SIZE,
    START_MARKER,
    decode_fast_telemetry,
)


# --- Simulated UART link (STM32 side) ---
STM32_HOST = "localhost"
STM32_PORT = 9000

STM32_RECONNECT_MIN_DELAY_SECONDS = 1
STM32_RECONNECT_MAX_DELAY_SECONDS = 10
STM32_SOCKET_TIMEOUT_SECONDS = 1.0

PACKET_SIZE = HEADER_SIZE + PAYLOAD_SIZE + CRC_SIZE
START_MARKER_BYTES = struct.pack("<H", START_MARKER)

# --- MQTT link (pit-wall side) ---
BROKER = "localhost"
PORT = 1883
TELEMETRY_TOPIC = "umf1/car01/telemetry/fast"
STATUS_TOPIC = "umf1/car01/gateway/status"

STATUS_INTERVAL_SECONDS = 1.0

MQTT_RECONNECT_MIN_DELAY_SECONDS = 1
MQTT_RECONNECT_MAX_DELAY_SECONDS = 30

packet_count = 0
decode_error_count = 0
dropped_count = 0
is_mqtt_connected = False


# ---------------------------------------------------------------------
# UART framing
# ---------------------------------------------------------------------

def extract_packets(buffer):
    """Pull complete, valid fast-telemetry packets out of a raw byte
    buffer received over the simulated UART link.

    Real UART delivers an unbroken stream of bytes with no message
    boundaries, so the receiver has to find the start marker itself,
    wait until enough bytes have arrived for a full packet, and verify
    the CRC. A marker byte sequence that turns up inside random data
    (a false positive) or a corrupted packet is treated as noise: we
    skip one byte and keep searching, which lets the stream resync
    after any corruption or lost bytes rather than getting stuck.

    Returns (decoded_packets, remaining_buffer, errors_found)
    """
    decoded_packets = []
    errors_found = 0

    while True:
        marker_index = buffer.find(START_MARKER_BYTES)

        if marker_index == -1:
            # No marker anywhere in the buffer. Keep the last byte in
            # case it's the first half of a marker that hasn't fully
            # arrived yet; discard everything before it as noise.
            if len(buffer) > 1:
                errors_found += 1
            buffer = buffer[-1:]
            break

        if marker_index > 0:
            # Bytes before the marker are noise / a partial packet
            # we'll never complete -- discard them.
            errors_found += 1
            buffer = buffer[marker_index:]

        if len(buffer) < PACKET_SIZE:
            # Marker found, but the rest of the packet hasn't arrived
            # yet. Wait for more bytes on the next read.
            break

        candidate = buffer[:PACKET_SIZE]

        try:
            decoded_packets.append(decode_fast_telemetry(candidate))
            buffer = buffer[PACKET_SIZE:]
        except ValueError:
            # Looked like a marker but the packet didn't validate
            # (false positive, or a genuinely corrupted packet).
            # Step past just this marker byte and keep searching.
            errors_found += 1
            buffer = buffer[1:]

    return decoded_packets, buffer, errors_found


def connect_to_stm32():
    """Connect to the simulated STM32 UART link, retrying with
    exponential backoff until it succeeds. Mirrors how the MQTT client
    handles reconnection, but for the UART side of the gateway.
    """
    delay = STM32_RECONNECT_MIN_DELAY_SECONDS

    while True:
        try:
            uart_socket = socket.socket(
                socket.AF_INET, socket.SOCK_STREAM
            )
            uart_socket.settimeout(STM32_SOCKET_TIMEOUT_SECONDS)
            uart_socket.connect((STM32_HOST, STM32_PORT))

            print("Virtual ESP32 connected to STM32 UART link")
            return uart_socket

        except (ConnectionRefusedError, OSError) as error:
            print(
                f"STM32 UART link unavailable ({error}). "
                f"Retrying in {delay}s..."
            )
            time.sleep(delay)
            delay = min(delay * 2, STM32_RECONNECT_MAX_DELAY_SECONDS)


# ---------------------------------------------------------------------
# MQTT link
# ---------------------------------------------------------------------

def publish_status(client):
    """Publish a heartbeat status message covering both links: the
    UART link to the STM32 and the MQTT link to the pit wall.

    retain=True so a client that subscribes late still immediately
    sees the last known status instead of waiting for the next update.
    """
    status_message = {
        "online": True,
        "packet_count": packet_count,
        "decode_error_count": decode_error_count,
        "dropped_count": dropped_count,
        "timestamp_ms": int(time.time() * 1000),
    }

    client.publish(
        STATUS_TOPIC,
        json.dumps(status_message),
        qos=1,
        retain=True,
    )


def on_connect(client, userdata, flags, reason_code, properties):
    global is_mqtt_connected

    if reason_code == 0:
        is_mqtt_connected = True
        print("Virtual ESP32 connected to Mosquitto")
        publish_status(client)
    else:
        is_mqtt_connected = False
        print(f"Virtual ESP32 MQTT connection failed: {reason_code}")


def on_disconnect(client, userdata, flags, reason_code, properties):
    global is_mqtt_connected

    is_mqtt_connected = False

    if reason_code == 0:
        print("Virtual ESP32 disconnected from Mosquitto")
    else:
        print(
            f"Virtual ESP32 lost MQTT connection unexpectedly "
            f"(reason: {reason_code}). Reconnecting..."
        )


mqtt_client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="umf1-virtual-esp32"
)

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect

# Last Will and Testament: if this client disconnects without a clean
# shutdown (crash, Wi-Fi drop, power loss), the broker publishes this
# on our behalf, so anything watching the status topic can reliably
# detect the gateway going offline even if it never gets to say so.
mqtt_client.will_set(
    STATUS_TOPIC,
    payload=json.dumps({
        "online": False,
        "packet_count": packet_count,
        "decode_error_count": decode_error_count,
        "dropped_count": dropped_count,
    }),
    qos=1,
    retain=True,
)

mqtt_client.reconnect_delay_set(
    min_delay=MQTT_RECONNECT_MIN_DELAY_SECONDS,
    max_delay=MQTT_RECONNECT_MAX_DELAY_SECONDS,
)


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------

def main():
    global packet_count, decode_error_count, dropped_count

    print("Virtual ESP32 starting...")
    mqtt_client.connect(BROKER, PORT)
    mqtt_client.loop_start()

    uart_socket = connect_to_stm32()
    buffer = b""
    last_status_time = time.monotonic()

    print("Receiving simulated UART byte stream")
    print("Press Control + C to stop")

    try:
        while True:
            try:
                chunk = uart_socket.recv(1024)

                if chunk == b"":
                    # Peer closed the connection cleanly.
                    raise ConnectionResetError("STM32 link closed")

                buffer += chunk

            except socket.timeout:
                # No data this cycle -- still fine, loop back around
                # so we can publish status on schedule below.
                chunk = None

            except (ConnectionResetError, OSError) as error:
                print(f"STM32 UART link lost ({error}). Reconnecting...")
                uart_socket.close()
                uart_socket = connect_to_stm32()
                buffer = b""
                continue

            if chunk:
                decoded_packets, buffer, errors = extract_packets(buffer)
                decode_error_count += errors

                for decoded_telemetry in decoded_packets:
                    if is_mqtt_connected:
                        mqtt_client.publish(
                            TELEMETRY_TOPIC,
                            json.dumps(decoded_telemetry),
                        )
                        packet_count += 1

                        print(
                            f"Packet {decoded_telemetry['sequence']}: "
                            f"throttle="
                            f"{decoded_telemetry['throttle_percent']}%, "
                            f"brake={decoded_telemetry['brake_bar']} bar, "
                            f"speed={decoded_telemetry['speed_kmh']} km/h"
                        )
                    else:
                        # Decoded fine, but nowhere to publish it right
                        # now -- same as a real ESP32 with no Wi-Fi.
                        dropped_count += 1
                        print(
                            f"Packet {decoded_telemetry['sequence']}: "
                            f"DROPPED (MQTT not connected)"
                        )

            now = time.monotonic()
            if now - last_status_time >= STATUS_INTERVAL_SECONDS:
                if is_mqtt_connected:
                    publish_status(mqtt_client)
                last_status_time = now

    except KeyboardInterrupt:
        print()
        print("Virtual ESP32 stopped")

    finally:
        if is_mqtt_connected:
            status_message = {
                "online": False,
                "packet_count": packet_count,
                "decode_error_count": decode_error_count,
                "dropped_count": dropped_count,
            }
            mqtt_client.publish(
                STATUS_TOPIC,
                json.dumps(status_message),
                qos=1,
                retain=True,
            )
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        uart_socket.close()


if __name__ == "__main__":
    main()
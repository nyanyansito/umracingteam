import json
import socket
import struct
import time
import uuid
import os

import paho.mqtt.client as mqtt
from mqtt_runtime import start_mqtt
from sequence_tracker import SequenceTracker

from telemetry_packet import (
    CRC_SIZE,
    HEADER_SIZE,
    PAYLOAD_SIZE,
    START_MARKER,
    decode_fast_telemetry,
)


# --- Simulated UART link (STM32 side) ---
from telemetry_config import STM32_HOST, STM32_PORT

STM32_RECONNECT_MIN_DELAY_SECONDS = 1
STM32_RECONNECT_MAX_DELAY_SECONDS = 10
STM32_SOCKET_TIMEOUT_SECONDS = 0.1

PACKET_SIZE = HEADER_SIZE + PAYLOAD_SIZE + CRC_SIZE
START_MARKER_BYTES = struct.pack("<H", START_MARKER)

# --- MQTT link (pit-wall side) ---
from telemetry_config import BROKER, PORT
TELEMETRY_TOPIC = "umf1/car01/telemetry/fast"
STATUS_TOPIC = "umf1/car01/gateway/status"

STATUS_INTERVAL_SECONDS = 1.0

MQTT_RECONNECT_MIN_DELAY_SECONDS = 1
MQTT_RECONNECT_MAX_DELAY_SECONDS = 30

packet_count = 0
decode_error_count = 0
dropped_count = 0
is_mqtt_connected = False
stm32_connected = False
last_packet_time = None
TELEMETRY_FRESHNESS_SECONDS = 2.0


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
    """One bounded connection attempt; main schedules retries and heartbeats."""
    uart_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        uart_socket.settimeout(STM32_SOCKET_TIMEOUT_SECONDS)
        uart_socket.connect((STM32_HOST, STM32_PORT))
    except BaseException:
        uart_socket.close()
        raise
    return uart_socket


def build_status(now=None, online=True):
    now = time.monotonic() if now is None else now
    age_ms = None if last_packet_time is None else max(0, int((now - last_packet_time) * 1000))
    return {
        "online": online,
        "stm32_connected": online and stm32_connected,
        "telemetry_fresh": online and stm32_connected and age_ms is not None and age_ms <= TELEMETRY_FRESHNESS_SECONDS * 1000,
        "last_valid_packet_age_ms": age_ms,
        "packet_count": packet_count,
        "decode_error_count": decode_error_count,
        "dropped_count": dropped_count,
        "wall_time_ms": int(time.time() * 1000),
    }


def publish_status(client):
    return client.publish(STATUS_TOPIC, json.dumps(build_status()), qos=1, retain=True)


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
        "stm32_connected": False,
        "telemetry_fresh": False,
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
    global stm32_connected, last_packet_time

    uart_socket = None
    buffer = b""
    reconnect_at = 0
    reconnect_delay = STM32_RECONNECT_MIN_DELAY_SECONDS
    last_status_time = -STATUS_INTERVAL_SECONDS
    stream_session = None
    tracker = SequenceTracker()
    try:
        start_mqtt(mqtt_client, BROKER, PORT)
        while True:
            now = time.monotonic()
            if now - last_status_time >= STATUS_INTERVAL_SECONDS:
                if is_mqtt_connected:
                    publish_status(mqtt_client)
                last_status_time = now

            if uart_socket is None:
                if now < reconnect_at:
                    time.sleep(0.01)
                    continue
                try:
                    uart_socket = connect_to_stm32()
                except OSError as error:
                    print(f"STM32 unavailable ({error}); retrying in {reconnect_delay}s")
                    reconnect_at = time.monotonic() + reconnect_delay
                    reconnect_delay = min(reconnect_delay * 2, STM32_RECONNECT_MAX_DELAY_SECONDS)
                    continue
                stm32_connected = True
                last_packet_time = None
                reconnect_delay = STM32_RECONNECT_MIN_DELAY_SECONDS
                stream_session = os.environ.get("UMR_SIM_SESSION_ID") or uuid.uuid4().hex
                tracker = SequenceTracker()
                buffer = b""

            try:
                chunk = uart_socket.recv(1024)
                if not chunk:
                    raise ConnectionResetError("STM32 link closed")
            except socket.timeout:
                continue
            except OSError as error:
                print(f"STM32 link lost ({error})")
                uart_socket.close()
                uart_socket = None
                stm32_connected = False
                last_packet_time = None
                buffer = b""
                reconnect_at = time.monotonic() + reconnect_delay
                if is_mqtt_connected:
                    publish_status(mqtt_client)
                continue

            decoded, buffer, errors = extract_packets(buffer + chunk)
            decode_error_count += errors
            for telemetry in decoded:
                last_packet_time = time.monotonic()
                sequence_result = tracker.check(telemetry["sequence"], telemetry["timestamp_ms"])
                if sequence_result["status"] == "reset":
                    stream_session = uuid.uuid4().hex
                telemetry["session_id"] = stream_session
                if is_mqtt_connected:
                    result = mqtt_client.publish(TELEMETRY_TOPIC, json.dumps(telemetry))
                    if result.rc == mqtt.MQTT_ERR_SUCCESS:
                        packet_count += 1
                    else:
                        dropped_count += 1
                else:
                    dropped_count += 1
    except KeyboardInterrupt:
        print("Virtual ESP32 stopped")
    finally:
        stm32_connected = False
        if uart_socket is not None:
            uart_socket.close()
        if is_mqtt_connected:
            result = mqtt_client.publish(STATUS_TOPIC, json.dumps(build_status(online=False)), qos=1, retain=True)
            result.wait_for_publish(timeout=1)
        mqtt_client.disconnect()
        mqtt_client.loop_stop()


if __name__ == "__main__":
    main()

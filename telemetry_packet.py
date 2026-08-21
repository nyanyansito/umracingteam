import binascii
import struct


START_MARKER = 0xAA55
PROTOCOL_VERSION = 1
FAST_TELEMETRY_MESSAGE_TYPE = 1

HEADER_FORMAT = "<HBBHHI"
PAYLOAD_FORMAT = "<HHhH"
CRC_FORMAT = "<H"

HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FORMAT)
CRC_SIZE = struct.calcsize(CRC_FORMAT)


def calculate_crc(data):
    return binascii.crc_hqx(data, 0xFFFF)


def encode_fast_telemetry(telemetry):
    throttle_raw = round(telemetry["throttle_percent"] * 100)
    brake_raw = round(telemetry["brake_bar"] * 100)
    steering_raw = round(telemetry["steering_degrees"] * 10)
    speed_raw = round(telemetry["speed_kmh"] * 100)

    payload = struct.pack(
        PAYLOAD_FORMAT,
        throttle_raw,
        brake_raw,
        steering_raw,
        speed_raw,
    )

    header = struct.pack(
        HEADER_FORMAT,
        START_MARKER,
        PROTOCOL_VERSION,
        FAST_TELEMETRY_MESSAGE_TYPE,
        len(payload),
        telemetry["sequence"],
        telemetry["timestamp_ms"],
    )

    packet_without_crc = header + payload
    crc = calculate_crc(packet_without_crc)

    return packet_without_crc + struct.pack(CRC_FORMAT, crc)


def decode_fast_telemetry(packet):
    expected_size = HEADER_SIZE + PAYLOAD_SIZE + CRC_SIZE

    if len(packet) != expected_size:
        raise ValueError("Incorrect packet size")

    packet_without_crc = packet[:-CRC_SIZE]
    received_crc = struct.unpack(CRC_FORMAT, packet[-CRC_SIZE:])[0]
    calculated_crc = calculate_crc(packet_without_crc)

    if received_crc != calculated_crc:
        raise ValueError("CRC check failed")

    header = struct.unpack(
        HEADER_FORMAT,
        packet[:HEADER_SIZE],
    )

    start_marker = header[0]
    protocol_version = header[1]
    message_type = header[2]
    payload_length = header[3]
    sequence = header[4]
    timestamp_ms = header[5]

    if start_marker != START_MARKER:
        raise ValueError("Invalid start marker")

    if protocol_version != PROTOCOL_VERSION:
        raise ValueError("Unsupported protocol version")

    if message_type != FAST_TELEMETRY_MESSAGE_TYPE:
        raise ValueError("Incorrect message type")

    if payload_length != PAYLOAD_SIZE:
        raise ValueError("Incorrect payload length")

    payload_start = HEADER_SIZE
    payload_end = payload_start + PAYLOAD_SIZE

    throttle_raw, brake_raw, steering_raw, speed_raw = struct.unpack(
        PAYLOAD_FORMAT,
        packet[payload_start:payload_end],
    )

    return {
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "throttle_percent": throttle_raw / 100,
        "brake_bar": brake_raw / 100,
        "steering_degrees": steering_raw / 10,
        "speed_kmh": speed_raw / 100,
    }


if __name__ == "__main__":
    example = {
        "sequence": 25,
        "timestamp_ms": 5000,
        "throttle_percent": 42.5,
        "brake_bar": 75.25,
        "steering_degrees": -18.5,
        "speed_kmh": 63.4,
    }

    encoded_packet = encode_fast_telemetry(example)
    decoded_packet = decode_fast_telemetry(encoded_packet)

    print(f"Binary packet size: {len(encoded_packet)} bytes")
    print(f"Binary packet: {encoded_packet.hex()}")
    print(f"Decoded telemetry: {decoded_packet}")
import unittest

from telemetry_packet import (
    decode_fast_telemetry,
    encode_fast_telemetry,
)


class TelemetryPacketTests(unittest.TestCase):

    def setUp(self):
        self.telemetry = {
            "sequence": 25,
            "timestamp_ms": 5000,
            "throttle_percent": 42.5,
            "brake_bar": 75.25,
            "steering_degrees": -18.5,
            "speed_kmh": 63.4,
        }

    def test_valid_packet_can_be_decoded(self):
        packet = encode_fast_telemetry(self.telemetry)
        decoded = decode_fast_telemetry(packet)

        self.assertEqual(decoded["sequence"], 25)
        self.assertEqual(decoded["timestamp_ms"], 5000)
        self.assertEqual(decoded["throttle_percent"], 42.5)
        self.assertEqual(decoded["brake_bar"], 75.25)
        self.assertEqual(decoded["steering_degrees"], -18.5)
        self.assertEqual(decoded["speed_kmh"], 63.4)

    def test_packet_size_is_22_bytes(self):
        packet = encode_fast_telemetry(self.telemetry)

        self.assertEqual(len(packet), 22)

    def test_corrupted_packet_is_rejected(self):
        packet = bytearray(
            encode_fast_telemetry(self.telemetry)
        )

        packet[15] ^= 0x01

        with self.assertRaisesRegex(ValueError, "CRC check failed"):
            decode_fast_telemetry(bytes(packet))

    def test_incomplete_packet_is_rejected(self):
        packet = encode_fast_telemetry(self.telemetry)
        incomplete_packet = packet[:-1]

        with self.assertRaisesRegex(ValueError, "Incorrect packet size"):
            decode_fast_telemetry(incomplete_packet)


if __name__ == "__main__":
    unittest.main()
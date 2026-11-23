import unittest
from unittest.mock import patch

import dashboard
from hardware_bench import SerialBench, ThrottleCalibration


class ThrottleBenchTest(unittest.TestCase):
    def test_raw_throttle_frames_feed_the_card_and_history(self):
        source = SerialBench("COM5", throttle_only=True)
        source.receive(b"UMR_STEERING,1000\n")
        self.assertIsNone(source.sensor_snapshot()[2])
        source.receive(b"UMR_THROTTLE,456\r\n")
        with patch.object(dashboard, "hardware_source", source):
            card, _, _, _, figure, *_ = dashboard.update_hardware_cards(True)
        self.assertEqual(card, "57.0 mV")
        self.assertEqual(list(figure.data[0].y), [57.0])
        self.assertEqual(figure.data[0].line.color, "#22c55e")
        self.assertEqual(len(figure.data), 1)

    def test_calibrated_frames_keep_missing_and_open_readings_off_the_plot(self):
        source = SerialBench("COM5", ThrottleCalibration(57, 99), throttle_only=True)
        for frame in (b"456", b"624", b"792", b"2400", b""):
            source.receive(b"UMR_THROTTLE," + frame + b"\n")
        arrays, _, _ = source.sensor_history()
        self.assertEqual(arrays[1], [0, 50, 100, None, None])
        self.assertIsNone(source.sensor_snapshot()[2])

    def test_gps_mode_ignores_steering_and_needs_throttle_calibration(self):
        source = SerialBench("COM5")
        source.receive(b"UMR_THROTTLE,456\n")
        source.receive(b"UMR_STEERING,12000\n")
        arrays, revision, _ = source.sensor_history()
        self.assertEqual((arrays[1], revision), ([57.0], 1))

        rmc = b"GNRMC,043806.00,A,0307.0000,N,10139.0000,E,0.0,0.0,221126,,,A"
        checksum = 0
        for byte in rmc:
            checksum ^= byte
        source.receive(b"$" + rmc + f"*{checksum:02X}\r\n".encode())
        self.assertEqual(source.lap_snapshot()["hardware_gps_status"], "GPS fix · 3.116667°, 101.650000°")

        with patch.object(dashboard, "hardware_source", source):
            cards = dashboard.update_hardware_cards(True)
        self.assertEqual(list(cards[4].data[0].y), [])
        self.assertEqual(cards[-2], "Hardware data · GPS and pedal inputs")

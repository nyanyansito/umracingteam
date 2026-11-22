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
            card, _, steering, _, _, figure, *_ = dashboard.update_hardware_cards(True)
        self.assertEqual(card, "57.0 mV")
        self.assertEqual(steering, "—")
        self.assertEqual(list(figure.data[0].y), [57.0])
        self.assertEqual(list(figure.data[2].y), [None])

    def test_calibrated_frames_keep_missing_and_open_readings_off_the_plot(self):
        source = SerialBench("COM5", ThrottleCalibration(57, 99), throttle_only=True)
        for frame in (b"456", b"624", b"792", b"2400", b""):
            source.receive(b"UMR_THROTTLE," + frame + b"\n")
        arrays, _, _ = source.sensor_history()
        self.assertEqual(arrays[1], [0, 50, 100, None, None])
        self.assertIsNone(source.sensor_snapshot()[2])

    def test_steering_mode_still_uses_the_steering_frame(self):
        source = SerialBench("COM5")
        source.receive(b"UMR_THROTTLE,456\n")
        source.receive(b"UMR_STEERING,12000\n")
        arrays, _, _ = source.sensor_history()
        self.assertEqual(arrays[1], [None])
        self.assertEqual(arrays[3], [1.5])

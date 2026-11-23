from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import dashboard
from hardware_bench import SerialBench, ThrottleCalibration


class ThrottleBenchTest(unittest.TestCase):
    def test_dashboard_seeds_themed_pedals_and_ignores_disconnected_brake_frames(self):
        graph = dashboard.app.layout["telemetry-graphs"]

        def check_display(**_):
            initial = graph.figure
            self.assertEqual(initial.layout.paper_bgcolor, dashboard.plot_style["paper_bgcolor"])
            self.assertEqual([trace.yaxis for trace in initial.data], ["y", "y2"])
            self.assertIn("Waiting for calibrated throttle data", [note.text for note in initial.layout.annotations])
            self.assertIn("Brake sensor disconnected", [note.text for note in initial.layout.annotations])

            source = dashboard.hardware_source
            source.receive(b"BRAKE_V=0.588\n")
            source.receive(b"BRAKE_I2C_ERROR\n")
            source.receive(b"UMR_THROTTLE,3304\n")
            cards = dashboard.update_hardware_cards(False)
            self.assertEqual(cards[:2], ("100.0 %", "—"))
            self.assertIn("Brake sensor disconnected", cards)
            plot, _, _ = dashboard.update_sensor_graph(1, 0, None)
            self.assertEqual(plot.layout.paper_bgcolor, initial.layout.paper_bgcolor)
            self.assertEqual(list(plot.data[0].y), [100])
            self.assertEqual(list(plot.data[1].y), [])

        with (patch.object(graph, "figure", graph.figure),
              patch.object(SerialBench, "read"),
              patch.object(dashboard.app, "run", side_effect=check_display) as run):
            dashboard.main(["--hardware-port", "COM5", "--throttle-calibration-mv", "91.5", "413",
                            "--brake-disconnected"])
        run.assert_called_once()

    def test_dashboard_saves_and_reuses_calibration_for_the_same_port_and_mode(self):
        port = "socket://192.168.4.1:9000"
        saved = ThrottleCalibration(91.5, 413)
        with (TemporaryDirectory() as directory,
              patch.object(dashboard, "THROTTLE_CALIBRATION_DIR", Path(directory)),
              patch.object(SerialBench, "read"),
              patch.object(dashboard, "app") as app):
            launches = []
            app.run.side_effect = lambda **_: launches.append(dashboard.hardware_source.calibration)

            dashboard.main(["--hardware-port", port, "--throttle-calibration-mv", "91.5", "413",
                            "--save-throttle-calibration"])
            dashboard.main(["--hardware-port", port])
            dashboard.main(["--hardware-port", port, "--throttle-calibration-mv", "80", "70"])
            dashboard.main(["--hardware-port", port])
            dashboard.main(["--hardware-port", port, "--throttle-bench"])
            dashboard.main(["--hardware-port", "COM5"])

        self.assertEqual(launches, [saved, saved, ThrottleCalibration(80, 70), saved, None, None])

    def test_live_throttle_reads_above_the_bench_range(self):
        source = SerialBench("socket://192.168.4.1:9000")
        source.receive(b"UMR_THROTTLE,3304\n")
        with patch.object(dashboard, "hardware_source", source):
            card = dashboard.update_hardware_cards(False)
        self.assertIn("413.0 mV · Calibration required", card)

        source = SerialBench(source.port, ThrottleCalibration(91.5, 413))
        cases = [(732, "0.0 %"), (2018, "50.0 %"), (3304, "100.0 %"), (0, "—"), (32767, "—")]
        with patch.object(dashboard, "hardware_source", source):
            for counts, expected in cases:
                source.receive(f"UMR_THROTTLE,{counts}\n".encode())
                self.assertEqual(dashboard.update_hardware_cards(False)[0], expected)
        released, middle, pressed, shorted, saturated = [value for _, value in source.pedal_history()[0]]
        self.assertAlmostEqual(middle, 50)
        self.assertEqual((released, pressed, shorted, saturated), (0, 100, None, None))

        with self.assertRaisesRegex(ValueError, "between 5 and 250 mV"):
            ThrottleCalibration(91.5, 413, bench_only=True)

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

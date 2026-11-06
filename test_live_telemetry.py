"""Regression and integration coverage for live lap integrity and recovery."""
import copy
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import dashboard
import data_processing as dp
import esp32_simulator as gateway
from lap_analysis import LapAnalysis
from lap_timer import LapTimer
from lap_service import LapService, InputBuffer
from lap_stream import RemoteLapAnalysis
from sample_order import SampleOrder
from session_store import save_session, restore_session
from telemetry_views import lap_input_figure
import test_hardware_readiness as fixtures
from test_hardware_readiness import SAMPLE, message
from test_lap_analysis import drive, fix
from test_support import circle_timer, generate_gps


class LapIntegrityTests(unittest.TestCase):
    def analysis(self):
        analysis = LapAnalysis(timer=circle_timer())
        drive(analysis, 20, 100)
        return analysis

    def test_stationary_finish_jitter_never_becomes_a_reference(self):
        analysis = LapAnalysis(timer=LapTimer(finish_start=(-.001, 0), finish_end=(.001, 0)))
        for tick in range(603):
            analysis.update({"timestamp_ms": tick * 100, "sequence": tick, "session_id": "stationary",
                             "latitude": (.5 if tick % 201 == 1 else -.5) / 111320,
                             "longitude": 0, "gps_valid": True, "gps_speed_kmh": 0}, now=tick / 10)
        self.assertEqual(analysis.completed_count, 0)
        self.assertIsNone(analysis.reference_time)
        self.assertIn("Incomplete circuit", analysis.timer.invalid_reason)

    def test_late_packet_keeps_completed_and_active_lap(self):
        analysis = self.analysis()
        before = analysis.snapshot(now=100)
        analysis.update(fix(99.9), now=100.1)
        after = analysis.snapshot(now=100.1)
        for key in ("best", "last", "lap_key", "current", "history"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(analysis.timer.order.rejected, 1)

    def test_invalid_boot_id_cannot_erase_best(self):
        analysis = self.analysis()
        analysis.update({"gps_valid": False, "session_id": "bad", "timestamp_ms": 0}, now=100.1)
        self.assertEqual(analysis.best_time, 45)
        self.assertEqual(len(analysis.history), 1)

    def test_retired_boot_packets_do_not_switch_back(self):
        analysis = self.analysis()
        analysis.update(fix(0, session="new"), now=100.1)
        analysis.update(fix(100.1), now=100.2)
        self.assertEqual(analysis.session_id, "new")
        self.assertEqual(analysis.timer.order.timestamp, 0)

    def test_source_gap_does_not_connect_unrelated_approach_points(self):
        analysis = LapAnalysis(timer=circle_timer())
        drive(analysis, 20, 25)
        self.assertGreater(len(analysis.approach), 1)
        analysis.update(fix(30), now=30)
        self.assertEqual(len(analysis.approach), 1)

    def test_new_boot_clears_old_outlap_trace(self):
        analysis = LapAnalysis(timer=circle_timer())
        drive(analysis, 20, 25)
        analysis.update(fix(0, session="new"), now=26)
        self.assertEqual(len(analysis.approach), 1)

    def test_delta_reacquires_after_an_excursion(self):
        analysis = self.analysis()
        for tick in range(1001, 1151):
            seconds = tick / 10
            gps = fix(seconds)
            offset = max(0, min(30, (seconds - 100) * 15, (107 - seconds) * 15))
            angle = seconds / 45 * math.tau
            gps["latitude"] += offset * math.sin(angle) / 111320
            gps["longitude"] += offset * math.cos(angle) / (111320 * math.cos(math.radians(3.1215)))
            analysis.update(gps, now=seconds)
        self.assertAlmostEqual(analysis.delta, 0, places=2)
        self.assertTrue(any(delta is None for _, delta in analysis.delta_trace))

    def test_reacquisition_does_not_guess_between_adjacent_straights(self):
        analysis = LapAnalysis(timer=circle_timer())
        analysis.reference = [(0, 0, 0, 0), (100, 0, 10, 100), (100, 1, 20, 200), (0, 1, 30, 300)]
        analysis.reference_distances = [0, 100, 200, 300]
        analysis.match_lost = True
        self.assertIsNone(analysis.compare((50, .5), 5))

    def test_missing_ordered_checkpoint_rejects_a_full_crossing(self):
        timer = circle_timer()
        timer.checkpoints = [[0, 0, 10]]  # Not on the fixture's circuit.
        analysis = LapAnalysis(timer=timer)
        drive(analysis, 20, 100)
        self.assertEqual(analysis.completed_count, 0)

    def test_valid_ordered_checkpoints_allow_complete_lap(self):
        timer = circle_timer()
        timer.checkpoints = [[generate_gps(0, s)["longitude"], generate_gps(0, s)["latitude"], 10] for s in (11.25, 22.5, 33.75)]
        analysis = LapAnalysis(timer=timer)
        drive(analysis, 20, 100)
        self.assertEqual(analysis.completed_count, 1)

    def test_buffered_old_gps_is_not_labelled_live(self):
        analysis = self.analysis()
        analysis.update(fix(100.1), now=105.1)
        self.assertIsNotNone(analysis.timer.lap_start_ms)
        self.assertFalse(analysis.snapshot(now=105.1)["fresh"])
        self.assertFalse(analysis.clock_state(now=105.1)["running"])

    def test_selecting_slower_reference_preserves_session_best(self):
        analysis = self.analysis()
        drive(analysis, 100.1, 140.2, lambda t: 90 + (t - 90) * 45 / 50)
        analysis.select_reference(2)
        self.assertEqual(analysis.best_time, 45)
        self.assertAlmostEqual(analysis.reference_time, 50, places=2)
        self.assertEqual(analysis.reference_lap, 2)
        analysis.select_reference()
        self.assertEqual(analysis.reference_lap, 1)
        with self.assertRaises(ValueError):
            analysis.select_reference(999)


class FastIntegrityTests(unittest.TestCase):
    setUp = fixtures.DashboardTests.setUp
    histories = staticmethod(fixtures.DashboardTests.histories)
    healthy = fixtures.DashboardTests.healthy
    status = fixtures.DashboardTests.status

    def test_duplicate_source_data_does_not_refresh_health_or_history(self):
        self.healthy()
        with patch("time.monotonic", return_value=10):
            dashboard.on_message(None, None, message(SAMPLE))
            dashboard.on_processed_message(None, None, message(dp.build_processed_message(dp.FAST_TOPIC, SAMPLE)))
        self.assertIn("STALE", self.status(10))
        self.assertEqual(len(dashboard.time_history), 1)

    def test_reboot_starts_new_graph_and_late_old_boot_is_ignored(self):
        with patch("time.monotonic", return_value=0):
            dashboard.on_message(None, None, message({**SAMPLE, "session_id": "a"}))
            dashboard.on_message(None, None, message({**SAMPLE, "sequence": 0, "timestamp_ms": 0, "session_id": "b"}))
            dashboard.on_message(None, None, message({**SAMPLE, "sequence": 26, "timestamp_ms": 5010, "session_id": "a"}))
        self.assertEqual(list(dashboard.time_history), [0])
        self.assertEqual(dashboard.telemetry["session_id"], "b")

    def test_good_validation_cannot_certify_new_bad_sample(self):
        self.healthy()
        with patch("time.monotonic", return_value=.01):
            dashboard.on_message(None, None, message({**SAMPLE, "sequence": 26, "timestamp_ms": 5010, "throttle_percent": 300}))
            result = dashboard.update_dashboard(0)
        self.assertEqual(result[0], "300.00 %")
        self.assertNotIn("all channels in range", result[-2])

    def test_timestamp_rollover_keeps_graph_time_increasing(self):
        for sample in ({**SAMPLE, "sequence": 65535, "timestamp_ms": (1 << 32) - 10},
                       {**SAMPLE, "sequence": 0, "timestamp_ms": 5}):
            dashboard.on_message(None, None, message(sample))
        times = list(dashboard.time_history)
        self.assertGreater(times[1], times[0])

    def test_speed_limits_and_outliers_remain_visible(self):
        figure = dashboard.make_sensor_figure([0, 1], [0, 300], [0, 0], [0, 0], [115, 130])
        self.assertGreaterEqual(figure.layout.yaxis4.range[1], 130)
        self.assertGreaterEqual(figure.layout.yaxis.range[1], 300)

    def test_graph_appends_only_new_points_and_resets_on_new_session(self):
        self.healthy()
        figure, _, state = dashboard.update_sensor_graph(0, 0, None)
        dashboard.on_message(None, None, message({**SAMPLE, "sequence": 26, "timestamp_ms": 5010}))
        _, extension, state = dashboard.update_sensor_graph(1, 0, state)
        self.assertEqual(extension[0]["x"][0], [5.01])
        dashboard.on_message(None, None, message({**SAMPLE, "sequence": 0, "timestamp_ms": 0, "session_id": "new"}))
        figure, _, state = dashboard.update_sensor_graph(2, 0, state)
        self.assertEqual(list(figure.data[0].x), [0])

    def test_gateway_wall_clock_is_not_a_device_counter(self):
        processed = dp.build_processed_message(dp.GATEWAY_STATUS_TOPIC, gateway.build_status())
        self.assertFalse(processed["any_invalid_data"])
        self.assertIn("wall_time_ms", processed)


class SharedLapServiceTests(unittest.TestCase):
    def setUp(self):
        self.analysis = LapAnalysis(timer=circle_timer())
        drive(self.analysis, 20, 100)
        self.client = MagicMock()
        self.client.publish.return_value = SimpleNamespace(rc=0)
        self.service = LapService(self.client, path=None, analysis=self.analysis)

    def test_late_joining_viewer_has_same_lap_number_and_best(self):
        mirror = RemoteLapAnalysis()
        packet = self.service.publish_state(now=100)
        mirror.receive(json.loads(json.dumps(packet)), now=100)
        view = mirror.snapshot(now=100)
        self.assertEqual(view["lap_number"], 2)
        self.assertEqual(view["best"], 45)
        self.assertEqual(view["history"][0]["lap_number"], 1)

    def test_retained_state_is_not_live_until_a_current_update(self):
        mirror = RemoteLapAnalysis()
        mirror.receive(self.service.publish_state(now=100), retained=True, now=100)
        self.assertFalse(mirror.snapshot(now=100)["fresh"])
        self.analysis.update(fix(100.1), now=100.1)
        mirror.receive(self.service.publish_state(now=100.1), now=100.1)
        self.assertTrue(mirror.snapshot(now=100.1)["fresh"])
        self.assertFalse(mirror.snapshot(now=102)["fresh"])

    def test_missing_append_update_requires_snapshot_and_recovers(self):
        mirror = RemoteLapAnalysis()
        mirror.receive(self.service.publish_state(now=100), now=100)
        self.analysis.update(fix(100.1), now=100.1)
        self.service.publish_state(now=100.1)  # Deliberately dropped.
        self.analysis.update(fix(100.2), now=100.2)
        self.assertFalse(mirror.receive(self.service.publish_state(now=100.2), now=100.2))
        self.assertTrue(mirror.needs_snapshot)
        self.service.request_snapshot = True
        mirror.receive(self.service.publish_state(now=100.3), now=100.3)
        self.assertTrue(mirror.snapshot(now=100.3)["fresh"])
        self.assertEqual(len(mirror.state["current"]), len(self.analysis.current))

    def test_old_service_snapshot_cannot_replace_new_service(self):
        mirror = RemoteLapAnalysis()
        first = self.service.publish_state(now=100)
        mirror.receive(first, now=100)
        new = {**first, "stream": "new", "sequence": 0}
        mirror.receive(new, now=100.1)
        self.assertFalse(mirror.receive({**first, "sequence": 1000}, now=100.2))
        self.assertEqual(mirror.stream, "new")

    def test_bad_snapshot_does_not_replace_good_state(self):
        mirror = RemoteLapAnalysis()
        packet = self.service.publish_state(now=100)
        mirror.receive(packet, now=100)
        bad = copy.deepcopy(packet)
        bad["sequence"] += 1
        bad["state"]["current"] = [["bad", 1]]
        with self.assertRaises(ValueError):
            mirror.receive(bad, now=100.1)
        self.assertEqual(mirror.state["best"], 45)

    def test_append_payload_does_not_repeat_reference(self):
        self.service.publish_state(now=100)
        self.analysis.update(fix(100.1), now=100.1)
        packet = self.service.publish_state(now=100.1)
        self.assertNotIn("reference", packet["state"])
        self.assertEqual(len(packet["state"]["current"]["items"]), 1)
        self.assertLess(len(json.dumps(packet)), 3000)

    def test_reference_and_input_graphs_share_distance(self):
        from gps_simulator import generate_gps as simulated_gps
        analysis = LapAnalysis()
        for tick in range(1600):
            analysis.update(simulated_gps(tick, tick / 10), now=tick / 10)
        state = analysis.snapshot(now=159.9)
        figure = lap_input_figure(state, inspection=50)
        self.assertEqual(list(figure.data[1].x), state["current_distances"])
        self.assertEqual(list(figure.data[0].x), [p[3] for p in state["reference"]])
        self.assertTrue(all(isinstance(c["steering_degrees"], (int, float)) for c in state["current_channels"]))
        self.assertEqual(len(figure.layout.shapes), 4)


class PersistenceAndAlignmentTests(unittest.TestCase):
    def test_recording_keeps_lap_events_without_repeating_display_snapshots(self):
        from telemetry_logger import SessionLogger
        with tempfile.TemporaryDirectory() as folder:
            logger = SessionLogger(folder)
            snapshot = SimpleNamespace(topic="umf1/car01/telemetry/lap/state", payload=b"{}", qos=1, retain=False)
            event = SimpleNamespace(topic="umf1/car01/telemetry/lap", payload=b'{"lap_number":1}', qos=1, retain=False)
            logger.record(snapshot)
            logger.record(event)
            logger.close()
            records = [json.loads(line) for line in logger.jsonl_path.read_text().splitlines()]
            self.assertEqual([r["topic"] for r in records], [event.topic])
            logger = SessionLogger(folder, include_lap_state=True)
            logger.record(snapshot)
            logger.close()
            self.assertEqual(len(logger.jsonl_path.read_text().splitlines()), 1)

    def test_restart_restores_completed_laps_but_rearms_partial_lap(self):
        analysis = LapAnalysis(timer=circle_timer())
        drive(analysis, 20, 100)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            save_session(path, analysis)
            restored = LapAnalysis(timer=circle_timer())
            self.assertTrue(restore_session(path, restored))
            self.assertEqual(restored.best_time, 45)
            self.assertEqual(restored.current, [])
            self.assertFalse(restored.snapshot(now=101)["fresh"])
            drive(restored, 100.1, 180.2)
            self.assertEqual([p["lap_number"] for p in restored.history], [1, 2])

    def test_different_track_does_not_load_saved_reference(self):
        analysis = LapAnalysis(timer=circle_timer())
        drive(analysis, 20, 100)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            save_session(path, analysis)
            self.assertFalse(restore_session(path, LapAnalysis()))

    def test_input_alignment_requires_same_clock_and_nearby_timestamp(self):
        buffer = InputBuffer()
        buffer.add({**SAMPLE, "session_id": "car"})
        gps = {"session_id": "car", "timestamp_ms": 5050}
        self.assertEqual(buffer.align(gps)["throttle_percent"], SAMPLE["throttle_percent"])
        self.assertNotIn("throttle_percent", buffer.align({**gps, "session_id": "other"}))
        self.assertNotIn("throttle_percent", buffer.align({**gps, "timestamp_ms": 6000}))

    def test_embedded_inputs_take_precedence_over_nearby_fast_sample(self):
        buffer = InputBuffer()
        buffer.add({**SAMPLE, "session_id": "car"})
        gps = {**SAMPLE, "session_id": "car", "throttle_percent": 15}
        self.assertEqual(buffer.align(gps)["throttle_percent"], 15)

    def test_launched_simulators_use_same_acquisition_clock(self):
        from simulation_clock import SimulationClock
        with patch.dict("os.environ", {"UMR_SIM_START_SECONDS": "100"}), patch("time.monotonic", return_value=110):
            fast, gps = SimulationClock(.01), SimulationClock(.1)
            self.assertEqual(fast.elapsed, gps.elapsed)
        with patch("time.monotonic", return_value=115):
            self.assertEqual(fast.elapsed, 15)
            self.assertEqual(gps.elapsed, 15)

    def test_shared_clock_waits_before_emitting_its_first_sample(self):
        from simulation_clock import SimulationClock
        with patch.dict("os.environ", {"UMR_SIM_START_SECONDS": "103"}), patch("time.monotonic", return_value=100), patch("time.sleep") as sleep:
            clock = SimulationClock(.1)
            sleep.assert_called_once_with(3)
            self.assertEqual(clock.deadline, 103)


if __name__ == "__main__":
    unittest.main()

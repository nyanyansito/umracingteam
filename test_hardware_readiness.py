"""Regression coverage for malformed inputs, outages, resets and wire framing."""
import base64
import contextlib
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dashboard
import data_processing as dp
import esp32_simulator as gateway
from flag_alerts import AlertTracker
from gps_simulator import generate_gps
from lap_timer import LapTimer, segments_intersect
from mqtt_runtime import run_mqtt, start_mqtt
from replay_telemetry import replay_records
from sequence_tracker import SequenceTracker
from telemetry_logger import SessionLogger
from telemetry_packet import calculate_crc, encode_fast_telemetry, decode_fast_telemetry
from telemetry_validation import FAST_FIELDS

SAMPLE = {"sequence": 25, "timestamp_ms": 5000, "throttle_percent": 42.5,
          "brake_bar": 75.25, "steering_degrees": -18.5, "speed_kmh": 63.4}


def message(data, topic=dp.FAST_TOPIC, retain=False):
    return SimpleNamespace(payload=json.dumps(data).encode(), topic=topic, qos=0, retain=retain)


class ValidationTests(unittest.TestCase):
    def test_non_objects_do_not_escape_callback(self):
        for payload in (None, [], 2, "hello"):
            with self.subTest(payload=payload), contextlib.redirect_stdout(io.StringIO()):
                client = MagicMock()
                dp.on_message(client, None, message(payload))
                client.publish.assert_not_called()

    def test_invalid_fields_publish_flags_and_standard_json_then_recover(self):
        for value in (None, "55", {}, [], True, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value), contextlib.redirect_stdout(io.StringIO()):
                client = MagicMock()
                dp.on_message(client, None, message({**SAMPLE, "speed_kmh": value}))
                published = client.publish.call_args.args[1]
                result = json.loads(published, parse_constant=lambda token: self.fail(token))
                self.assertEqual(result["range_flags"]["speed_kmh"], "invalid")
                self.assertTrue(result["any_invalid_data"])
                dp.on_message(client, None, message(SAMPLE))
                recovered = json.loads(client.publish.call_args.args[1])
                self.assertFalse(recovered["any_invalid_data"])
                self.assertFalse(recovered["any_out_of_range"])

    def test_empty_packet_has_missing_channels_and_metadata(self):
        result = dp.build_processed_message(dp.FAST_TOPIC, {})
        self.assertTrue(result["any_invalid_data"])
        for field in (*FAST_FIELDS, "sequence", "timestamp_ms"):
            self.assertEqual(result["range_flags"][field], "missing")

    def test_boolean_channels_accept_only_booleans(self):
        self.assertEqual(dp.build_processed_message(dp.STATUS_TOPIC, {"online": True})["range_flags"]["online"], "ok")
        self.assertEqual(dp.build_processed_message(dp.STATUS_TOPIC, {"online": 1})["range_flags"]["online"], "invalid")

    def test_bad_metadata_is_flagged_and_not_forwarded(self):
        result = dp.build_processed_message(dp.FAST_TOPIC, {**SAMPLE, "sequence": None, "timestamp_ms": -1})
        self.assertNotIn("sequence", result)
        self.assertNotIn("timestamp_ms", result)
        self.assertTrue(result["any_invalid_data"])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        dashboard.telemetry.clear()
        dashboard.telemetry.update(SAMPLE)
        for history in self.histories():
            history.clear()
        dashboard.last_raw_time = None
        dashboard.raw_invalid = False
        dashboard.mqtt_connected = True
        dashboard.gateway_status = None
        dashboard.last_gateway_time = None
        dashboard.alert_tracker = AlertTracker()

    @staticmethod
    def histories():
        return (dashboard.time_history, dashboard.throttle_history, dashboard.brake_history,
                dashboard.steering_history, dashboard.speed_history)

    def healthy(self, now=0):
        with patch("time.monotonic", return_value=now):
            dashboard.on_message(None, None, message(SAMPLE))
            dashboard.on_processed_message(None, None, message(dp.build_processed_message(dp.FAST_TOPIC, SAMPLE)))

    def status(self, now):
        with patch("time.monotonic", return_value=now):
            return dashboard.update_dashboard(0)[-2]

    def test_partial_sample_leaves_all_state_unchanged(self):
        before = dashboard.telemetry.copy()
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard.on_message(None, None, message({"timestamp_ms": 1, "throttle_percent": 10}))
        self.assertEqual(dashboard.telemetry, before)
        self.assertEqual([len(x) for x in self.histories()], [0] * 5)
        self.assertTrue(dashboard.raw_invalid)

    def test_stale_and_processor_only_failure_cannot_stay_green(self):
        self.healthy()
        self.assertIn("all channels in range", self.status(0))
        self.assertIn("STALE", self.status(3))
        with patch("time.monotonic", return_value=3):
            dashboard.on_message(None, None, message(SAMPLE))
        self.assertIn("validation", self.status(3))
        self.assertNotIn("all channels in range", self.status(3))

    def test_warning_expiry_without_new_good_data_cannot_turn_green(self):
        self.healthy()
        bad = dp.build_processed_message(dp.FAST_TOPIC, {**SAMPLE, "throttle_percent": 300})
        with patch("time.monotonic", return_value=0):
            dashboard.on_processed_message(None, None, message(bad))
        self.assertIn("WARNING", self.status(0))
        self.assertNotIn("all channels in range", self.status(60))

    def test_invalid_processed_shapes_cannot_refresh_validation(self):
        for bad in ({}, {"range_flags": []}, None, {"range_flags": {field: "ok" for field in FAST_FIELDS}, "values": {}, "limits": {}}):
            with contextlib.redirect_stdout(io.StringIO()):
                dashboard.on_processed_message(None, None, message(bad))
            self.assertIsNone(dashboard.alert_tracker.last_processed_time)

    def test_retained_sample_is_not_live(self):
        with contextlib.redirect_stdout(io.StringIO()):
            dashboard.on_message(None, None, message(SAMPLE, retain=True))
        self.assertIsNone(dashboard.last_raw_time)

    def test_disconnect_invalidates_freshness_and_recovery_is_possible(self):
        self.healthy()
        dashboard.on_disconnect(None, None, None, 1, None)
        self.assertIn("Disconnected", self.status(0))
        dashboard.mqtt_connected = True
        self.healthy(1)
        self.assertIn("all channels in range", self.status(1))

    def test_gateway_link_and_status_expiry_override_green(self):
        self.healthy()
        status = {"online": True, "stm32_connected": False, "telemetry_fresh": False}
        with patch("time.monotonic", return_value=0):
            dashboard.on_gateway_status(None, None, message(status))
        self.assertIn("STM32", self.status(0))
        status.update(stm32_connected=True, telemetry_fresh=True)
        with patch("time.monotonic", return_value=0):
            dashboard.on_gateway_status(None, None, message(status))
        self.healthy(4)
        self.assertIn("gateway status", self.status(4))


class SequenceTests(unittest.TestCase):
    def test_gap_duplicate_late_and_recovery(self):
        tracker = SequenceTracker()
        tracker.check(100)
        self.assertEqual(tracker.check(99)["status"], "late")
        self.assertEqual(tracker.check(101)["status"], "ok")
        self.assertEqual(tracker.check(101)["status"], "duplicate")
        self.assertEqual(tracker.check(104)["missed_packets"], 2)

    def test_sequence_and_clock_rollover(self):
        tracker = SequenceTracker()
        tracker.check(65535, (1 << 32) - 10)
        self.assertEqual(tracker.check(0, 5)["status"], "ok")

    def test_restart_does_not_report_huge_gap(self):
        tracker = SequenceTracker()
        tracker.check(1000, 100000)
        self.assertEqual(tracker.check(0, 0), {"status": "reset", "missed_packets": 0})
        self.assertEqual(tracker.check(1, 10)["status"], "ok")

    def test_boot_id_and_old_boot_packets(self):
        tracker = SequenceTracker()
        tracker.check(1000, 100000, "old")
        self.assertEqual(tracker.check(5, 50, "new")["status"], "reset")
        self.assertEqual(tracker.check(1001, 100010, "old")["status"], "late")
        self.assertEqual(tracker.check(6, 60, "new")["status"], "ok")

    def test_invalid_sequences(self):
        for value in (-1, 65536, None, True, "1"):
            with self.assertRaises(ValueError):
                SequenceTracker().check(value)


class LapTests(unittest.TestCase):
    @staticmethod
    def feed(timer, start, end):
        result = []
        for tick in range(round(start * 10), round(end * 10) + 1):
            now = tick / 10
            lap = timer.update(generate_gps(tick % 65536, now), now=now)
            if lap:
                result.append(lap)
        return result

    def test_joining_mid_lap_waits_for_two_crossings(self):
        timer = LapTimer()
        self.assertEqual(self.feed(timer, 20, 45.1), [])
        laps = self.feed(timer, 45.2, 90.2)
        self.assertEqual(len(laps), 1)
        self.assertAlmostEqual(laps[0]["lap_time_seconds"], 45, places=2)

    def test_invalid_fix_gap_jump_and_restart_rearm(self):
        for interruption in ("invalid", "gap", "jump", "restart", "host_gap"):
            with self.subTest(interruption=interruption):
                timer = LapTimer()
                self.feed(timer, 44, 60)
                self.assertIsNotNone(timer.lap_start_ms)
                if interruption == "invalid":
                    timer.update({"gps_valid": False}, now=60.1)
                elif interruption == "gap":
                    timer.update(generate_gps(0, 91), now=91)
                elif interruption == "jump":
                    timer.update({**generate_gps(0, 60.1), "latitude": 4}, now=60.1)
                elif interruption == "host_gap":
                    timer.update(generate_gps(0, 60.1), now=62)
                else:
                    timer.update(generate_gps(0, 0), now=60.1)
                self.assertIsNone(timer.lap_start_ms)
                self.assertEqual(timer.lap_number, 0)

    def test_known_outage_does_not_publish_fabricated_lap(self):
        timer = LapTimer()
        timer.update(generate_gps(1, 44), now=44)
        timer.update({"gps_valid": False}, now=45)
        self.assertIsNone(timer.update(generate_gps(2, 91), now=91))

    def test_timestamp_rollover_preserves_continuity(self):
        timer = LapTimer()
        a = {**generate_gps(1, 44.9), "timestamp_ms": (1 << 32) - 50}
        b = {**generate_gps(2, 45.1), "timestamp_ms": 150}
        timer.update(a, now=0)
        timer.update(b, now=0.2)
        self.assertIsNotNone(timer.lap_start_ms)

    def test_disjoint_collinear_segments_do_not_intersect(self):
        self.assertFalse(segments_intersect((0, 0), (1, 0), (2, 0), (3, 0)))


class GatewayTests(unittest.TestCase):
    def test_status_distinguishes_silent_and_disconnected_stm32(self):
        with patch.object(gateway, "stm32_connected", True), patch.object(gateway, "last_packet_time", 0):
            self.assertTrue(gateway.build_status(now=1)["telemetry_fresh"])
            stale = gateway.build_status(now=3)
            self.assertTrue(stale["online"])
            self.assertFalse(stale["telemetry_fresh"])
            self.assertFalse(gateway.build_status(now=1, online=False)["stm32_connected"])
        with patch.object(gateway, "stm32_connected", False):
            self.assertFalse(gateway.build_status()["stm32_connected"])

    def test_heartbeat_continues_while_stm32_unavailable(self):
        clock = [0.0]
        def monotonic():
            clock[0] += 0.25
            if 6 < clock[0] < 6.5:
                raise KeyboardInterrupt
            return clock[0]
        client = MagicMock()
        with patch.object(gateway, "mqtt_client", client), patch.object(gateway, "is_mqtt_connected", True), patch.object(gateway, "stm32_connected", False), patch.object(gateway, "last_packet_time", None), patch.object(gateway, "start_mqtt"), patch.object(gateway, "connect_to_stm32", side_effect=ConnectionRefusedError), patch.object(gateway.time, "monotonic", side_effect=monotonic), patch.object(gateway.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            gateway.main()
        heartbeats = [json.loads(call.args[1]) for call in client.publish.call_args_list if call.args[0] == gateway.STATUS_TOPIC]
        self.assertGreaterEqual(len(heartbeats), 3)
        self.assertTrue(all(not status["stm32_connected"] for status in heartbeats))
        self.assertFalse(heartbeats[-1]["online"])

    def test_failed_connect_closes_socket(self):
        sock = MagicMock()
        sock.connect.side_effect = ConnectionRefusedError
        with patch.object(gateway.socket, "socket", return_value=sock):
            with self.assertRaises(ConnectionRefusedError):
                gateway.connect_to_stm32()
        sock.close.assert_called_once()

    def test_publish_failure_is_counted_as_dropped_and_session_is_attached(self):
        sock = MagicMock()
        sock.recv.side_effect = [encode_fast_telemetry(SAMPLE), KeyboardInterrupt]
        client = MagicMock()
        client.publish.return_value.rc = gateway.mqtt.MQTT_ERR_NO_CONN
        with patch.object(gateway, "mqtt_client", client), patch.object(gateway, "is_mqtt_connected", True), patch.object(gateway, "packet_count", 0), patch.object(gateway, "dropped_count", 0), patch.object(gateway, "stm32_connected", False), patch.object(gateway, "last_packet_time", None), patch.object(gateway, "start_mqtt"), patch.object(gateway, "connect_to_stm32", return_value=sock), contextlib.redirect_stdout(io.StringIO()):
            gateway.main()
            self.assertEqual(gateway.packet_count, 0)
            self.assertEqual(gateway.dropped_count, 1)
        publications = [json.loads(call.args[1]) for call in client.publish.call_args_list if call.args[0] == gateway.TELEMETRY_TOPIC]
        self.assertEqual(len(publications), 1)
        self.assertIsInstance(publications[0]["session_id"], str)
        sock.close.assert_called_once()

    def test_fragmentation_and_corruption_recovery(self):
        packet = encode_fast_telemetry(SAMPLE)
        for split in range(len(packet) + 1):
            first, buffer, _ = gateway.extract_packets(packet[:split])
            second, buffer, _ = gateway.extract_packets(buffer + packet[split:])
            self.assertEqual(first + second, [SAMPLE])
            self.assertEqual(buffer, b"")
        damaged = bytearray(packet)
        damaged[15] ^= 1
        found, _, errors = gateway.extract_packets(b"noise" + damaged + packet * 2)
        self.assertEqual(found, [SAMPLE, SAMPLE])
        self.assertGreater(errors, 0)

    def test_independent_crc_check_and_frozen_wire_vector(self):
        self.assertEqual(calculate_crc(b"123456789"), 0x29B1)
        packet = bytes.fromhex("55aa010108001900881300009a10651d47ffc418bed0")
        self.assertEqual(decode_fast_telemetry(packet), SAMPLE)
        self.assertEqual(encode_fast_telemetry(SAMPLE), packet)


class RuntimeAndRecordingTests(unittest.TestCase):
    def test_startup_is_asynchronous_and_first_failure_is_retryable(self):
        client = MagicMock()
        start_mqtt(client, "localhost", 1883)
        client.connect.assert_not_called()
        client.connect_async.assert_called_once_with("localhost", 1883)
        client.loop_start.assert_called_once()
        run_mqtt(client, "localhost", 1883)
        client.loop_forever.assert_called_once_with(retry_first_connection=True)
        client.disconnect.assert_called_once()

    def test_recording_preserves_all_topics_and_invalid_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = SessionLogger(folder)
            messages = [message(SAMPLE), message({"gps_valid": False}, dp.GPS_TOPIC),
                        message({"lap_number": 1}, dp.LAP_TOPIC),
                        message({"range_flags": {}}, dp.FAST_TOPIC + "/processed"),
                        SimpleNamespace(payload=b"\xff\x00", topic=dp.GATEWAY_STATUS_TOPIC, qos=1, retain=False)]
            for item in messages:
                logger.record(item)
            logger.close()
            rows = [json.loads(line) for line in logger.jsonl_path.read_text().splitlines()]
            self.assertEqual(len(rows), 5)
            for row, original in zip(rows, messages):
                self.assertEqual(base64.b64decode(row["payload_base64"]), original.payload)
                self.assertIn("received_monotonic_ns", row)
            self.assertEqual(len(logger.csv_path.read_text().splitlines()), 2)
            default_replay = list(replay_records(logger.jsonl_path))
            self.assertEqual(len(default_replay), 3)
            self.assertEqual(len(list(replay_records(logger.jsonl_path, all_topics=True))), 5)
            self.assertEqual(json.loads(list(replay_records(logger.csv_path))[0][2]), SAMPLE)

    def test_invalid_fast_sample_is_still_preserved_losslessly(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = SessionLogger(folder)
            bad = message({**SAMPLE, "speed_kmh": float("nan")})
            with contextlib.redirect_stdout(io.StringIO()):
                logger.record(bad)
            logger.close()
            row = json.loads(logger.jsonl_path.read_text(), parse_constant=lambda token: self.fail(token))
            self.assertEqual(base64.b64decode(row["payload_base64"]), bad.payload)
            self.assertIsNone(row["data"]["speed_kmh"])
            self.assertEqual(len(logger.csv_path.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()

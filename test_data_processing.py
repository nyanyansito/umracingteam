import tempfile
import unittest
from pathlib import Path

from data_processing import (
    build_channel_specs,
    build_processed_message,
    convert_raw_to_engineering,
    load_channel_rows,
    process_fields,
    validate_range,
    verify_passthrough,
    CHANNEL_SPECS,
    CSV_PATH,
    FAST_TOPIC,
    GPS_TOPIC,
    LAP_TOPIC,
    STATUS_TOPIC,
    GATEWAY_STATUS_TOPIC,
    PROCESSED_TOPIC_SUFFIX,
    RANGE_OK,
    RANGE_OUT_OF_RANGE,
)


class LoadChannelRowsFromCsvTests(unittest.TestCase):
    """Confirms the CSV is actually being read from disk, not assumed."""

    def test_real_csv_file_is_found(self):
        self.assertTrue(CSV_PATH.exists(), f"{CSV_PATH} should exist")

    def test_known_channel_row_matches_file_contents(self):
        rows = load_channel_rows(CSV_PATH)

        self.assertIn("throttle_position", rows)
        self.assertEqual(rows["throttle_position"]["min_value"], 0)
        self.assertEqual(rows["throttle_position"]["max_value"], 100)
        self.assertEqual(rows["throttle_position"]["status"], "implemented")

    def test_blank_max_value_becomes_none(self):
        rows = load_channel_rows(CSV_PATH)

        # packet_count has a blank max_value column in the CSV.
        self.assertIsNone(rows["packet_count"]["max_value"])

    def test_planned_channel_status_is_read_correctly(self):
        rows = load_channel_rows(CSV_PATH)

        self.assertEqual(rows["engine_rpm"]["status"], "planned")


class BuildChannelSpecsFromCsvTests(unittest.TestCase):
    """If the CSV changes, CHANNEL_SPECS should reflect it -- these
    tests write a temporary CSV to prove the values really come from
    the file, not from anything hardcoded in the module.
    """

    def _write_temp_csv(self, rows_text):
        header = (
            "channel_id,channel_name,description,unit,data_type,"
            "sample_rate_hz,telemetry_rate_hz,min_value,max_value,"
            "mqtt_topic,status\n"
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        )
        tmp.write(header + rows_text)
        tmp.close()
        return Path(tmp.name)

    def test_changing_max_value_in_csv_changes_the_spec(self):
        csv_path = self._write_temp_csv(
            "1,throttle_position,Throttle,percent,uint16,1000,100,0,150,"
            "umf1/car01/telemetry/fast,implemented\n"
            "2,brake_pressure,Brake,bar,uint16,1000,100,0,100,"
            "umf1/car01/telemetry/fast,implemented\n"
            "3,steering_angle,Steering,degrees,int16,200,100,-45,45,"
            "umf1/car01/telemetry/fast,implemented\n"
            "4,vehicle_speed,Speed,km/h,uint16,100,100,0,120,"
            "umf1/car01/telemetry/fast,implemented\n"
            "5,latitude,Lat,decimal_degrees,float64,10,10,-90,90,"
            "umf1/car01/telemetry/gps,implemented\n"
            "6,longitude,Lon,decimal_degrees,float64,10,10,-180,180,"
            "umf1/car01/telemetry/gps,implemented\n"
            "7,gps_speed,GPS speed,km/h,float32,10,10,0,120,"
            "umf1/car01/telemetry/gps,implemented\n"
            "8,gps_valid,Valid,boolean,bool,10,10,0,1,"
            "umf1/car01/telemetry/gps,implemented\n"
            "11,lap_number,Lap,count,uint16,,on_lap_completion,0,999,"
            "umf1/car01/telemetry/lap,implemented\n"
            "12,lap_time,Lap time,seconds,float64,,on_lap_completion,0,600,"
            "umf1/car01/telemetry/lap,implemented\n"
            "13,online,Online,boolean,bool,,1,0,1,"
            "umf1/car01/status,implemented\n"
            "14,packet_count,Count,count,uint32,,1,0,,"
            "umf1/car01/gateway/status,implemented\n"
            "15,decode_error_count,Errors,count,uint32,,1,0,,"
            "umf1/car01/gateway/status,implemented\n"
            "16,dropped_count,Dropped,count,uint32,,1,0,,"
            "umf1/car01/gateway/status,implemented\n"
        )

        try:
            specs = build_channel_specs(csv_path)
            # This CSV set throttle_position's max_value to 150 instead
            # of the real repo's 100 -- the spec should follow it.
            self.assertEqual(specs["throttle_percent"]["max_value"], 150)
        finally:
            csv_path.unlink()

    def test_missing_expected_channel_raises_clear_error(self):
        csv_path = self._write_temp_csv(
            "1,throttle_position,Throttle,percent,uint16,1000,100,0,100,"
            "umf1/car01/telemetry/fast,implemented\n"
            # brake_pressure deliberately omitted
        )

        try:
            with self.assertRaises(KeyError):
                build_channel_specs(csv_path)
        finally:
            csv_path.unlink()

    def test_planned_channels_are_excluded_from_specs(self):
        # engine_rpm (channel 21) is status=planned in the real CSV and
        # has no wire-field mapping, so it should never appear here.
        self.assertNotIn("engine_rpm", CHANNEL_SPECS)


class ConvertRawToEngineeringTests(unittest.TestCase):
    """Cross-checked against the worked examples in telemetry-packet.md."""

    def test_throttle_scaling(self):
        self.assertEqual(convert_raw_to_engineering(4250, 100), 42.5)

    def test_brake_scaling(self):
        self.assertEqual(convert_raw_to_engineering(7525, 100), 75.25)

    def test_steering_scaling_negative(self):
        self.assertEqual(convert_raw_to_engineering(-185, 10), -18.5)

    def test_speed_scaling(self):
        self.assertEqual(convert_raw_to_engineering(6340, 100), 63.4)

    def test_zero_scale_factor_raises(self):
        with self.assertRaises(ValueError):
            convert_raw_to_engineering(100, 0)

    def test_none_scale_factor_raises(self):
        with self.assertRaises(ValueError):
            convert_raw_to_engineering(100, None)


class ValidateRangeTests(unittest.TestCase):

    def test_value_in_range(self):
        self.assertTrue(validate_range(50, 0, 100))

    def test_value_below_minimum(self):
        self.assertFalse(validate_range(-1, 0, 100))

    def test_value_above_maximum(self):
        self.assertFalse(validate_range(101, 0, 100))

    def test_minimum_boundary_is_inclusive(self):
        self.assertTrue(validate_range(0, 0, 100))

    def test_maximum_boundary_is_inclusive(self):
        self.assertTrue(validate_range(100, 0, 100))

    def test_none_max_value_is_unbounded_above(self):
        self.assertTrue(validate_range(999_999, 0, None))

    def test_none_min_value_is_unbounded_below(self):
        self.assertTrue(validate_range(-999_999, None, 100))


class ProcessFieldsTests(unittest.TestCase):

    def test_all_fields_in_range(self):
        payload = {
            "throttle_percent": 42.5,
            "brake_bar": 0.0,
            "steering_degrees": -18.5,
            "speed_kmh": 63.4,
        }

        processed, flags = process_fields(payload, [
            "throttle_percent", "brake_bar", "steering_degrees", "speed_kmh"
        ])

        self.assertEqual(processed, payload)
        self.assertTrue(all(flag == RANGE_OK for flag in flags.values()))

    def test_out_of_range_value_is_flagged_not_modified(self):
        payload = {"throttle_percent": 150.0}  # above CSV max_value of 100

        processed, flags = process_fields(payload, ["throttle_percent"])

        self.assertEqual(processed["throttle_percent"], 150.0)
        self.assertEqual(flags["throttle_percent"], RANGE_OUT_OF_RANGE)

    def test_missing_field_is_skipped_not_raised(self):
        payload = {"throttle_percent": 42.5}

        processed, flags = process_fields(
            payload, ["throttle_percent", "brake_bar"]
        )

        self.assertIn("throttle_percent", processed)
        self.assertNotIn("brake_bar", processed)

    def test_field_not_in_channel_specs_is_skipped(self):
        # engine_rpm has no wire-field mapping (planned, not implemented).
        payload = {"engine_rpm": 8000}

        processed, flags = process_fields(payload, ["engine_rpm"])

        self.assertEqual(processed, {})
        self.assertEqual(flags, {})


class ProcessedTopicSuffixTests(unittest.TestCase):

    def test_suffix_is_appended_per_source_topic_not_shared(self):
        # Each source topic gets its own processed topic, e.g.
        # umf1/car01/telemetry/fast/processed, so mqtt_subscriber.py's
        # per-topic sequence tracker never sees two unrelated counters
        # (or a missing one) mixed together on the same topic.
        self.assertEqual(
            FAST_TOPIC + PROCESSED_TOPIC_SUFFIX,
            "umf1/car01/telemetry/fast/processed",
        )
        self.assertEqual(
            LAP_TOPIC + PROCESSED_TOPIC_SUFFIX,
            "umf1/car01/telemetry/lap/processed",
        )


class BuildProcessedMessageTests(unittest.TestCase):

    def test_fast_topic_message_structure(self):
        payload = {
            "sequence": 125,
            "timestamp_ms": 15240,
            "throttle_percent": 42.5,
            "brake_bar": 0.0,
            "steering_degrees": -18.5,
            "speed_kmh": 63.4,
        }

        message = build_processed_message(FAST_TOPIC, payload)

        self.assertEqual(message["source_topic"], FAST_TOPIC)
        self.assertFalse(message["any_out_of_range"])

    def test_gps_topic_message_structure(self):
        payload = {
            "sequence": 88,
            "timestamp_ms": 15200,
            "latitude": 3.1215,
            "longitude": 101.6532,
            "gps_speed_kmh": 63.4,
            "gps_valid": True,
        }

        message = build_processed_message(GPS_TOPIC, payload)

        self.assertEqual(message["values"]["gps_valid"], True)
        self.assertFalse(message["any_out_of_range"])

    def test_lap_topic_message_structure(self):
        payload = {"lap_number": 3, "lap_time_seconds": 47.128}

        message = build_processed_message(LAP_TOPIC, payload)

        self.assertFalse(message["any_out_of_range"])

    def test_status_topic_message_structure(self):
        payload = {"online": True, "source": "python"}

        message = build_processed_message(STATUS_TOPIC, payload)

        self.assertEqual(message["values"]["online"], True)

    def test_gateway_status_topic_message_structure(self):
        payload = {
            "online": True,
            "packet_count": 15420,
            "decode_error_count": 2,
            "dropped_count": 0,
        }

        message = build_processed_message(GATEWAY_STATUS_TOPIC, payload)

        self.assertEqual(message["values"]["packet_count"], 15420)
        self.assertFalse(message["any_out_of_range"])

    def test_any_out_of_range_flag_set_when_one_field_bad(self):
        payload = {
            "throttle_percent": 999,
            "brake_bar": 0.0,
            "steering_degrees": 0.0,
            "speed_kmh": 10.0,
        }

        message = build_processed_message(FAST_TOPIC, payload)

        self.assertTrue(message["any_out_of_range"])
        self.assertEqual(
            message["range_flags"]["throttle_percent"], RANGE_OUT_OF_RANGE
        )

    def test_unhandled_topic_returns_none(self):
        message = build_processed_message("umf1/car01/events", {"fault": "x"})
        self.assertIsNone(message)

    def test_sequence_omitted_when_not_in_source_payload(self):
        # lap/status payloads never carry a sequence number -- the key
        # should be absent entirely, never present as None, since a
        # present-but-None sequence is what crashed mqtt_subscriber.py.
        payload = {"lap_number": 1, "lap_time_seconds": 30.0}

        message = build_processed_message(LAP_TOPIC, payload)

        self.assertNotIn("sequence", message)

    def test_sequence_included_when_present_in_source_payload(self):
        payload = {
            "sequence": 42,
            "throttle_percent": 10.0,
            "brake_bar": 0.0,
            "steering_degrees": 0.0,
            "speed_kmh": 10.0,
        }

        message = build_processed_message(FAST_TOPIC, payload)

        self.assertEqual(message["sequence"], 42)

    def test_timestamp_ms_omitted_when_not_in_source_payload(self):
        # umf1/car01/status payloads (online/source) never carry
        # timestamp_ms either.
        payload = {"online": True, "source": "python"}

        message = build_processed_message(STATUS_TOPIC, payload)

        self.assertNotIn("timestamp_ms", message)


class VerifyPassthroughTests(unittest.TestCase):

    def test_matches_when_values_unchanged(self):
        raw_payload = {
            "throttle_percent": 42.5,
            "brake_bar": 0.0,
            "steering_degrees": -18.5,
            "speed_kmh": 63.4,
        }

        message = build_processed_message(FAST_TOPIC, raw_payload)

        self.assertTrue(verify_passthrough(raw_payload, message))

    def test_still_matches_when_a_value_is_out_of_range(self):
        raw_payload = {
            "throttle_percent": 999,
            "brake_bar": 0.0,
            "steering_degrees": 0.0,
            "speed_kmh": 10.0,
        }

        message = build_processed_message(FAST_TOPIC, raw_payload)

        self.assertTrue(verify_passthrough(raw_payload, message))

    def test_detects_a_tampered_value(self):
        raw_payload = {
            "throttle_percent": 42.5,
            "brake_bar": 0.0,
            "steering_degrees": -18.5,
            "speed_kmh": 63.4,
        }

        message = build_processed_message(FAST_TOPIC, raw_payload)
        message["values"]["throttle_percent"] = 41.0

        self.assertFalse(verify_passthrough(raw_payload, message))


if __name__ == "__main__":
    unittest.main()
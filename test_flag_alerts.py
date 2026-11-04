import unittest

from flag_alerts import AlertTracker, describe_alert, HOLD_SECONDS


def processed(flags, values, limits=None):
    return {
        "range_flags": flags,
        "values": values,
        "limits": limits or {},
    }


BAD_THROTTLE = processed(
    {"throttle_percent": "out_of_range", "brake_bar": "ok"},
    {"throttle_percent": 300.0, "brake_bar": 0.0},
    {
        "throttle_percent": {"min": 0, "max": 100},
        "brake_bar": {"min": 0, "max": 100},
    },
)

ALL_OK = processed(
    {"throttle_percent": "ok", "brake_bar": "ok"},
    {"throttle_percent": 50.0, "brake_bar": 0.0},
)


class AlertTrackerTests(unittest.TestCase):

    def test_starts_with_no_alerts_and_no_data_seen(self):
        tracker = AlertTracker()

        self.assertEqual(tracker.active(now=0), {})
        self.assertFalse(tracker.processed_seen)

    def test_in_range_message_creates_no_alert(self):
        tracker = AlertTracker()
        tracker.update(ALL_OK, now=0)

        self.assertEqual(tracker.active(now=0), {})
        self.assertTrue(tracker.processed_seen)

    def test_out_of_range_field_becomes_active_alert(self):
        tracker = AlertTracker()
        tracker.update(BAD_THROTTLE, now=10)

        active = tracker.active(now=10)

        self.assertEqual(list(active), ["throttle_percent"])
        self.assertEqual(active["throttle_percent"]["value"], 300.0)
        self.assertEqual(active["throttle_percent"]["max"], 100)

    def test_only_the_bad_field_is_alerted(self):
        tracker = AlertTracker()
        tracker.update(BAD_THROTTLE, now=0)

        self.assertNotIn("brake_bar", tracker.active(now=0))

    def test_alert_survives_following_normal_messages(self):
        # The real-life case: one bad packet, then normal packets 100 ms
        # later. The warning must NOT vanish instantly.
        tracker = AlertTracker()
        tracker.update(BAD_THROTTLE, now=0)
        tracker.update(ALL_OK, now=0.1)
        tracker.update(ALL_OK, now=0.2)

        self.assertIn("throttle_percent", tracker.active(now=0.3))

    def test_alert_expires_after_hold_time(self):
        tracker = AlertTracker(hold_seconds=5)
        tracker.update(BAD_THROTTLE, now=0)

        self.assertIn("throttle_percent", tracker.active(now=4.9))
        self.assertEqual(tracker.active(now=5.1), {})

    def test_new_bad_value_restarts_the_timer(self):
        tracker = AlertTracker(hold_seconds=5)
        tracker.update(BAD_THROTTLE, now=0)
        tracker.update(BAD_THROTTLE, now=4)

        self.assertIn("throttle_percent", tracker.active(now=8))
        self.assertEqual(tracker.active(now=9.5), {})

    def test_age_is_reported(self):
        tracker = AlertTracker()
        tracker.update(BAD_THROTTLE, now=10)

        self.assertAlmostEqual(
            tracker.active(now=12)["throttle_percent"]["age_seconds"], 2
        )

    def test_default_hold_time_is_a_few_seconds(self):
        self.assertGreaterEqual(HOLD_SECONDS, 3)

    def test_message_without_limits_still_alerts(self):
        # Older data_processing.py versions did not send "limits".
        tracker = AlertTracker()
        tracker.update(
            processed(
                {"speed_kmh": "out_of_range"}, {"speed_kmh": 999.0}
            ),
            now=0,
        )

        alert = tracker.active(now=0)["speed_kmh"]
        self.assertIsNone(alert["min"])
        self.assertIsNone(alert["max"])


class DescribeAlertTests(unittest.TestCase):

    def test_sentence_includes_value_limits_and_age(self):
        text = describe_alert(
            {"value": 300.0, "min": 0, "max": 100, "age_seconds": 2.2}
        )

        self.assertEqual(text, "Got 300.00 (allowed 0 to 100) - 2s ago")

    def test_negative_limits(self):
        text = describe_alert(
            {"value": -99.0, "min": -45, "max": 45, "age_seconds": 0}
        )

        self.assertIn("allowed -45 to 45", text)

    def test_missing_limits_are_left_out(self):
        text = describe_alert(
            {"value": 5.0, "min": None, "max": None, "age_seconds": 1}
        )

        self.assertEqual(text, "Got 5.00 - 1s ago")


if __name__ == "__main__":
    unittest.main()
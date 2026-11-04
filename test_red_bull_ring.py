import math
import unittest
import dashboard
from gps_simulator import generate_gps
from stm32_simulator import generate_telemetry
from lap_analysis import LapAnalysis
from simulated_track import BASE_LAP_SECONDS, TRACK_LENGTH, ORIGIN, LON_METRES, sample_track


class RedBullRingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analysis = LapAnalysis()
        cls.end = int(BASE_LAP_SECONDS * 2.1 * 10) / 10
        for n in range(round(cls.end * 10) + 1):
            t = n / 10
            cls.analysis.update(generate_gps(n % 65536, t), now=t)

    def test_coordinates_length_and_varying_inputs(self):
        self.assertTrue(4200 < TRACK_LENGTH < 4400)
        self.assertTrue(47 < ORIGIN[1] < 48)
        samples = [generate_telemetry(t, t) for t in range(round(BASE_LAP_SECONDS))]
        for t, sample in enumerate(samples):
            self.assertAlmostEqual(generate_gps(t, t)["gps_speed_kmh"], sample["speed_kmh"], places=2)
            self.assertTrue(0 <= sample["speed_kmh"] <= 120)
        self.assertGreater(max(p["speed_kmh"] for p in samples) - min(p["speed_kmh"] for p in samples), 40)
        self.assertGreater(max(p["brake_bar"] for p in samples), 20)
        self.assertLess(min(p["steering_degrees"] for p in samples), -5)
        self.assertGreater(max(p["steering_degrees"] for p in samples), 5)

    def test_position_speed_consistency(self):
        for t in (10, 30, 80, 140, 155, 200):
            a, b = sample_track(t), sample_track(t + .001)
            distance = math.hypot((b["longitude"] - a["longitude"]) * LON_METRES,
                                  (b["latitude"] - a["latitude"]) * 111320)
            self.assertAlmostEqual(distance / .001 * 3.6, a["speed_kmh"], delta=.1)

    def test_finish_crossings_varied_laps_and_top_speed(self):
        state = self.analysis.snapshot(now=self.end)
        self.assertEqual(state["completed_count"], 2)
        self.assertGreater(abs(state["history"][0]["lap_time_seconds"] - state["history"][1]["lap_time_seconds"]), 2)
        for lap in state["history"]:
            self.assertTrue(110 < lap["top_speed_kmh"] < 120)
        self.assertEqual(state["best_top_speed"], state["history"][0]["top_speed_kmh"])
        self.assertGreaterEqual(state["session_top_speed"], state["best_top_speed"])
        self.assertGreater(len(state["track_outline"]), 50)

    def test_table_best_speed_and_delta(self):
        state = self.analysis.snapshot(now=self.end)
        text = str(dashboard.lap_table(state))
        for label in ("Whole stint", "SESSION BEST", "TOP KPH", "BEST", "2 laps"):
            self.assertIn(label, text)
        self.assertIn(f"{state['history'][1]['lap_time_seconds'] - state['best']:+.3f}", text)

    def test_gap_resets_current_top_but_keeps_session_top(self):
        analysis = LapAnalysis()
        for n in range(100):
            analysis.update(generate_gps(n, n / 10), now=n / 10)
        maximum = analysis.session_top_speed
        analysis.update({"gps_valid": False}, now=10)
        self.assertIsNone(analysis.lap_top_speed)
        self.assertEqual(analysis.session_top_speed, maximum)


if __name__ == "__main__":
    unittest.main()

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard
from gps_simulator import generate_gps
from lap_analysis import LapAnalysis, input_mode


class TrackInputTests(unittest.TestCase):
    def test_classification_and_brake_priority(self):
        for sample, expected in (({"throttle_percent": 80, "brake_bar": 0}, "throttle"),
                                 ({"throttle_percent": 80, "brake_bar": 15}, "brake"),
                                 ({"throttle_percent": 0, "brake_bar": 0}, "coast"),
                                 ({}, "unknown"),
                                 ({"throttle_percent": float("nan"), "brake_bar": 0}, "unknown")):
            self.assertEqual(input_mode(sample), expected)

    def test_segment_colors_do_not_bridge_unrelated_sections(self):
        state = {"current": [(0, 0, 0), (1, 0, 1), (2, 0, 2), (3, 0, 3)],
                 "current_inputs": ["throttle", "brake", "coast", "coast"],
                 "reference": [], "fresh": True, "delta_trace": []}
        track, _ = dashboard.lap_figures(state)
        traces = {trace.name: trace for trace in track.data}
        self.assertEqual(traces["Throttle"].line.color, "#22c55e")
        self.assertEqual(traces["Braking"].line.color, "#ef4444")
        self.assertEqual(traces["Coasting"].line.color, "#a3a3a3")
        self.assertEqual(list(traces["Braking"].x), [1, 2, None])

    def test_full_simulated_reference_preserves_matching_input_samples(self):
        analysis = LapAnalysis()
        for n in range(1600):
            analysis.update(generate_gps(n, n / 10), now=n / 10)
        state = analysis.snapshot(now=159.9)
        self.assertEqual(len(state["reference"]), len(state["reference_inputs"]))
        self.assertEqual(len(state["current"]), len(state["current_inputs"]))
        self.assertIn("brake", state["reference_inputs"])
        self.assertIn("throttle", state["reference_inputs"])
        self.assertNotIn("unknown", state["reference_inputs"])

    def test_lightweight_clock_anchor_pauses_and_has_no_chart_data(self):
        analysis = LapAnalysis()
        for n in range(100):
            analysis.update(generate_gps(n, n / 10), now=n / 10)
        state = analysis.clock_state(now=9.95)
        self.assertTrue(state["running"])
        self.assertAlmostEqual(state["age"], .05)
        self.assertNotIn("reference", state)
        self.assertFalse(analysis.clock_state(now=12)["running"])
        analysis.interrupt()
        self.assertIsNone(analysis.clock_state(now=12)["elapsed"])

    def test_asset_and_clock_route_are_available(self):
        client = dashboard.app.server.test_client()
        asset = client.get("/assets/lap_clock.js")
        try:
            self.assertEqual(asset.status_code, 200)
        finally:
            asset.close()
        with patch.object(dashboard, "lap_analysis", LapAnalysis()):
            response = client.post("/_dash-update-component", json={
                "output": "lap-clock-state.data", "outputs": {"id": "lap-clock-state", "property": "data"},
                "inputs": [{"id": "clock-update", "property": "n_intervals", "value": 0}],
                "changedPropIds": ["clock-update.n_intervals"]})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["response"]["lap-clock-state"]["data"]["elapsed"])

    @unittest.skipUnless(shutil.which("node"), "Node is optional; needed for browser-clock regression tests")
    def test_browser_clock_javascript(self):
        subprocess.run([shutil.which("node"), str(Path(__file__).with_name("test_lap_clock.js"))],
                       check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()

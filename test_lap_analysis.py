import unittest
from unittest.mock import patch

import dashboard
from test_support import generate_gps, circle_timer
from lap_analysis import LapAnalysis


def fix(seconds, phase=None, session="test"):
    gps = generate_gps(round(seconds * 10) % 65536, seconds if phase is None else phase)
    gps.update(timestamp_ms=round(seconds * 1000) % (1 << 32), session_id=session)
    return gps


def drive(analysis, start, end, phase=None):
    for tick in range(round(start * 10), round(end * 10) + 1):
        seconds = tick / 10
        analysis.update(fix(seconds, None if phase is None else phase(seconds)), now=seconds)


class LapAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.analysis = LapAnalysis(timer=circle_timer())

    def test_partial_lap_never_becomes_reference(self):
        drive(self.analysis, 20, 45.2)
        state = self.analysis.snapshot(now=45.2)
        self.assertIsNone(state["best"])
        self.assertIsNone(state["delta"])
        self.assertEqual(state["history"], [])
        self.assertAlmostEqual(state["elapsed"], .2, places=2)

    def test_complete_lap_creates_reference_and_equal_pace_delta(self):
        drive(self.analysis, 20, 100)
        state = self.analysis.snapshot(now=100)
        self.assertAlmostEqual(state["best"], 45, places=2)
        self.assertEqual(state["lap_number"], 2)
        self.assertEqual(len(state["history"]), 1)
        self.assertAlmostEqual(state["delta"], 0, places=2)
        self.assertGreater(len(state["reference"]), 400)

    def test_faster_pace_is_negative_and_promotes_new_best(self):
        drive(self.analysis, 20, 90)
        fast_phase = lambda seconds: 90 + (seconds - 90) * 45 / 40
        drive(self.analysis, 90.1, 110, fast_phase)
        state = self.analysis.snapshot(now=110)
        self.assertAlmostEqual(state["delta"], -2.5, delta=.04)
        self.assertAlmostEqual(state["best"], 45, places=2)
        drive(self.analysis, 110.1, 130.2, fast_phase)
        state = self.analysis.snapshot(now=130.2)
        self.assertAlmostEqual(state["best"], 40, delta=.02)
        self.assertEqual(state["reference_lap"], 2)
        self.assertAlmostEqual(state["history"][-1]["delta"], -5, delta=.02)

    def test_slower_pace_is_positive_and_keeps_reference(self):
        drive(self.analysis, 20, 90)
        slow_phase = lambda seconds: 90 + (seconds - 90) * 45 / 50
        drive(self.analysis, 90.1, 115, slow_phase)
        self.assertAlmostEqual(self.analysis.snapshot(now=115)["delta"], 2.5, delta=.04)
        drive(self.analysis, 115.1, 140.2, slow_phase)
        self.assertAlmostEqual(self.analysis.reference_time, 45, delta=.02)
        self.assertEqual(self.analysis.reference_lap, 1)

    def test_gps_loss_discards_partial_lap_but_preserves_completed_best(self):
        drive(self.analysis, 20, 100)
        self.analysis.update({"gps_valid": False}, now=100.1)
        state = self.analysis.snapshot(now=100.1)
        self.assertFalse(state["fresh"])
        self.assertIsNone(state["elapsed"])
        self.assertIsNone(state["delta"])
        self.assertAlmostEqual(state["best"], 45, places=2)
        drive(self.analysis, 110, 135.2)
        self.assertEqual(len(self.analysis.history), 1)

    def test_receive_timeout_hides_delta_without_resetting_lap(self):
        drive(self.analysis, 20, 100)
        state = self.analysis.snapshot(now=102)
        self.assertFalse(state["fresh"])
        self.assertIsNone(state["delta"])
        self.assertAlmostEqual(state["elapsed"], 10, places=2)
        self.assertIsNotNone(self.analysis.timer.lap_start_ms)

    def test_restart_clears_reference_and_lap_history(self):
        drive(self.analysis, 20, 100)
        self.analysis.update(fix(0, session="new"), now=100.1)
        self.assertEqual(self.analysis.history, self.analysis.history.__class__(maxlen=50))
        self.assertIsNone(self.analysis.reference_time)
        self.assertEqual(self.analysis.current, [])

    def test_display_stall_then_contiguous_gps_completes_same_lap(self):
        drive(self.analysis, 20, 100)
        baseline = self.analysis.timer.lap_start_ms
        for now in (102, 102.5, 103):
            state = self.analysis.snapshot(now=now)
            self.assertFalse(state["fresh"])
            self.assertAlmostEqual(state["elapsed"], 10, places=2)
            self.assertEqual(self.analysis.timer.lap_start_ms, baseline)
        for tick in range(1001, 1353):
            source = tick / 10
            self.analysis.update(fix(source), now=source + 3)
        state = self.analysis.snapshot(now=138.2)
        self.assertEqual(len(state["history"]), 2)
        self.assertAlmostEqual(state["last"], 45, places=2)
        self.assertAlmostEqual(state["elapsed"], .2, places=2)

    def test_actual_source_gap_still_discards_partial_lap(self):
        drive(self.analysis, 20, 100)
        self.analysis.snapshot(now=102)
        self.analysis.update(fix(103), now=103)
        self.assertIsNone(self.analysis.timer.lap_start_ms)
        self.assertEqual(self.analysis.current, [])
        self.assertEqual(len(self.analysis.history), 1)

    def test_off_reference_position_has_no_delta(self):
        drive(self.analysis, 20, 100)
        self.assertIsNone(self.analysis.compare((10000, 10000), 10))

    def test_duplicate_fix_does_not_extend_history(self):
        drive(self.analysis, 20, 100)
        before = len(self.analysis.current)
        self.analysis.update(fix(100), now=100.01)
        self.assertEqual(len(self.analysis.current), before)


class LapDashboardTests(unittest.TestCase):
    def test_empty_and_populated_views_render(self):
        analysis = LapAnalysis(timer=circle_timer())
        with patch.object(dashboard, "lap_analysis", analysis), patch("time.monotonic", return_value=0):
            cards, status, track, delta, history = dashboard.update_lap_dashboard(0)
            self.assertEqual(len(cards), 5)
            self.assertIn("unavailable", status)
        drive(analysis, 20, 100)
        with patch.object(dashboard, "lap_analysis", analysis), patch("time.monotonic", return_value=100):
            cards, status, track, delta, history = dashboard.update_lap_dashboard(0)
            self.assertIn("best lap 1", status)
            self.assertEqual(len(track.data), 3)
            self.assertGreater(len(delta.data[0].x), 0)
            self.assertIsNotNone(history)

    def test_layout_and_callback_route_work_without_broker(self):
        client = dashboard.app.server.test_client()
        self.assertEqual(client.get("/_dash-layout").status_code, 200)
        deps = client.get("/_dash-dependencies").get_json()
        callback = next(dep for dep in deps if "lap-metrics" in dep["output"])
        outputs = [{"id": name, "property": prop} for name, prop in (
            ("lap-metrics", "children"), ("lap-status", "children"),
            ("track-trace", "figure"), ("lap-delta-graph", "figure"), ("lap-history", "children"))]
        response = client.post("/_dash-update-component", json={
            "output": callback["output"], "outputs": outputs,
            "inputs": [{"id": "lap-update", "property": "n_intervals", "value": 0}],
            "changedPropIds": ["lap-update.n_intervals"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("track-trace", response.get_json()["response"])

    def test_malformed_gps_callback_pauses_lap_analysis(self):
        analysis = LapAnalysis(timer=circle_timer())
        drive(analysis, 20, 100)
        from types import SimpleNamespace
        with patch.object(dashboard, "lap_analysis", analysis):
            dashboard.on_gps_message(None, None, SimpleNamespace(payload=b"null", retain=False))
        self.assertIsNone(analysis.snapshot(now=100)["delta"])


if __name__ == "__main__":
    unittest.main()

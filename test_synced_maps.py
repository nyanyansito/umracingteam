import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard
from course_from_log import extract
from hardware_bench import SerialBench, ThrottleCalibration
from pedal_sync import MOD, PedalClock
from venue_map import ROADS, VENUE, area_status, build_map, load_course


def rmc(seconds, latitude="0322.2105", status="A"):
    body = f"GNRMC,1200{seconds},{status},{latitude},N,10134.4445,E,2.0,0.0,241126,,,A".encode()
    checksum = 0
    for byte in body:
        checksum ^= byte
    return b"$" + body + f"*{checksum:02X}\r\n".encode()


class SyncTests(unittest.TestCase):
    def make_session(self, lag=0):
        s = SerialBench("test", ThrottleCalibration(100, 400))
        for i in range(50):
            tick = 1002 + 10*i
            key = "T" if i % 2 == 0 else "B"
            raw = 2400 if key == "T" else (16000 if tick < 1300 else 2700)
            with patch("hardware_bench.time.monotonic", return_value=200+tick/1000):
                s.receive(f"UMR_ADC,{key},{tick},{raw}\n".encode())
        with patch("hardware_bench.time.monotonic", return_value=202+lag):
            s.receive(b"UMR_RMC,1280,1000," + rmc("01.20"))
            s.receive(b"UMR_RMC,1480,1000," + rmc("01.40", "0322.2106"))
        return s

    def test_arrival_jitter_and_all_views(self):
        a, b = self.make_session(), self.make_session(.37)
        self.assertEqual(list(a.map_points), list(b.map_points))
        first, last = a.map_points
        self.assertGreater(first[3], 5)
        self.assertLess(last[3], 5)
        self.assertAlmostEqual(first[6], .198)
        self.assertLessEqual(a.match_offset, 15)
        for mode in ("online", "offline", "track"):
            fig, meta = build_map(mode, [first, last], True, [], True, None, 1)
            self.assertEqual(list(fig.data[0].lon), [first[0], last[0], None])
            self.assertEqual(fig.data[4].customdata[0], last[6])
            self.assertEqual(meta["latest"][0], last[5])
            if mode != "online":
                self.assertEqual(fig.layout.map.style, f"/assets/{mode}-map-style.json")

    def test_missing_pulse_gap_invalid_adc_and_wrap(self):
        clock = PedalClock()
        clock.add("T", MOD-4, 15)
        clock.add("B", 4, 20)
        values, _, offset = clock.match(80, MOD-100, 100)
        self.assertEqual((values["brake_percent"], offset), (20, 4))
        self.assertIsNone(clock.match(80, None, 100)[0]["brake_percent"])
        self.assertIsNone(clock.match(500, MOD-100, 100)[2])
        clock.add("T", 202, None)
        self.assertIsNone(clock.match(250, 0, 200)[0]["throttle_percent"])

    def test_disconnect_and_course_capture(self):
        source = self.make_session()
        source.receive(rmc("01.60", status="V"))
        points, fresh, *_ = source.map_snapshot()
        self.assertFalse(fresh)
        source.connection_changed("offline")
        self.assertEqual(source.map_snapshot()[0], points[-1:])
        self.assertFalse(source.pedal_clock.samples["T"])
        source.receive(b"UMR_BOOT\n")
        self.assertIsNone(source.match_offset)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "walk.geojson")
            import json
            path.write_text(json.dumps(extract([rmc("01.20"), rmc("01.40", "0322.2106")])))
            self.assertEqual(len(load_course(path)), 2)
        with self.assertRaises(ValueError):
            extract([rmc("01.20"), rmc("01.40", status="V")])

    def test_offline_assets_and_updates(self):
        self.assertGreater(len(ROADS["features"]), 0)
        client = dashboard.app.server.test_client()
        for name in ("venue-roads.geojson", "competition-layout.png"):
            with client.get("/assets/"+name) as response:
                self.assertEqual(response.status_code, 200)
        points = list(self.make_session().map_points)
        self.assertEqual(area_status("offline", points, []), "")
        self.assertIn("Outside", area_status("offline", [(0, 0)], []))
        _, old = build_map("offline", points[:1], True, [], True, None, 3)
        delta, _ = build_map("offline", points, True, [], True, old, 3)
        ops = delta.to_plotly_json()["operations"]
        self.assertTrue(any(op["operation"] == "Extend" for op in ops))
        self.assertFalse(any("layers" in op["location"] for op in ops))
        self.assertEqual(area_status("track", [(0, 0)], [VENUE, VENUE]), "Outside course area")

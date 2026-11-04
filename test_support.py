"""Independent circular fixtures for deterministic timing regression tests."""
import math
from lap_timer import LapTimer


def circle_timer():
    scale = 111320 * math.cos(math.radians(3.1215))
    return LapTimer(finish_start=(101.6532 + 70 / scale, 3.1215),
                    finish_end=(101.6532 + 130 / scale, 3.1215))


def generate_gps(sequence, elapsed_seconds):
    angle = elapsed_seconds / 45 * math.tau
    return {"sequence": sequence, "timestamp_ms": int(elapsed_seconds * 1000) % (1 << 32),
            "latitude": 3.1215 + 100 * math.sin(angle) / 111320,
            "longitude": 101.6532 + 100 * math.cos(angle) / (111320 * math.cos(math.radians(3.1215))),
            "gps_speed_kmh": math.tau * 100 / 45 * 3.6, "gps_valid": True}

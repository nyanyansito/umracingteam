"""Red Bull Ring centreline with synthetic, varying club-car pace (not F1 physics)."""
import bisect
import json
import math
from pathlib import Path

TRACK_NAME = "Red Bull Ring · Spielberg, Austria"
COORDINATES = json.loads((Path(__file__).parent / "tracks/red_bull_ring.geojson").read_text())["features"][0]["geometry"]["coordinates"]
ORIGIN = COORDINATES[0]
LON_METRES = 111320 * math.cos(math.radians(ORIGIN[1]))
POINTS = [((lon - ORIGIN[0]) * LON_METRES, (lat - ORIGIN[1]) * 111320) for lon, lat in COORDINATES]
LENGTHS = [math.dist(a, b) for a, b in zip(POINTS, POINTS[1:])]
DISTANCES = [0.0]
for length in LENGTHS:
    DISTANCES.append(DISTANCES[-1] + length)
TRACK_LENGTH = DISTANCES[-1]


def turn(index):
    count = len(POINTS) - 1
    a, b, c = POINTS[(index - 1) % count], POINTS[index % count], POINTS[(index + 1) % count]
    u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    return math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])


# Slow for bends, accelerate between them, and limit acceleration/braking.
SPEEDS = [max(40, 116 - abs(turn(i)) * 65) / 3.6 for i in range(len(POINTS) - 1)]
for _ in range(4):
    for i in range(len(SPEEDS)):
        j = (i + 1) % len(SPEEDS)
        SPEEDS[j] = min(SPEEDS[j], math.sqrt(SPEEDS[i] ** 2 + 2 * 2.5 * LENGTHS[i]))
    for i in reversed(range(len(SPEEDS))):
        j = (i + 1) % len(SPEEDS)
        SPEEDS[i] = min(SPEEDS[i], math.sqrt(SPEEDS[j] ** 2 + 2 * 5 * LENGTHS[i]))
SPEEDS.append(SPEEDS[0])
TIMES = [0.0]
for i, distance in enumerate(LENGTHS):
    TIMES.append(TIMES[-1] + 2 * distance / (SPEEDS[i] + SPEEDS[i + 1]))
BASE_LAP_SECONDS = TIMES[-1]
PACE_FACTORS = (1.0, 1.02, .99, 1.01, .98)
LAP_ENDS = [0.0]
for pace in PACE_FACTORS:
    LAP_ENDS.append(LAP_ENDS[-1] + BASE_LAP_SECONDS / pace)


def sample_track(elapsed_seconds):
    # Start just before the finish so a complete timed lap starts promptly.
    cycle = (max(0, elapsed_seconds) + BASE_LAP_SECONDS - 1) % LAP_ENDS[-1]
    lap = min(len(PACE_FACTORS) - 1, bisect.bisect_right(LAP_ENDS, cycle) - 1)
    pace = PACE_FACTORS[lap]
    phase = (cycle - LAP_ENDS[lap]) * pace
    i = min(len(LENGTHS) - 1, bisect.bisect_right(TIMES, phase) - 1)
    dt = phase - TIMES[i]
    duration = TIMES[i + 1] - TIMES[i]
    acceleration = (SPEEDS[i + 1] - SPEEDS[i]) / duration
    fraction = min(1, max(0, (SPEEDS[i] * dt + .5 * acceleration * dt * dt) / LENGTHS[i]))
    a, b = COORDINATES[i], COORDINATES[i + 1]
    return {
        "longitude": a[0] + fraction * (b[0] - a[0]),
        "latitude": a[1] + fraction * (b[1] - a[1]),
        "speed_kmh": (SPEEDS[i] + acceleration * dt) * pace * 3.6,
        "throttle_percent": max(0, min(100, 55 + acceleration * 25)),
        "brake_bar": min(100, max(0, -acceleration * 16)),
        "steering_degrees": max(-45, min(45, -math.degrees(turn(i)) * (1 - fraction) - math.degrees(turn(i + 1)) * fraction)),
    }


# Directed cross-track gate: travel through start/finish is negative to positive.
dx, dy = POINTS[1][0] - POINTS[0][0], POINTS[1][1] - POINTS[0][1]
norm = math.hypot(dx, dy)
gate_x, gate_y = dy / norm * 25, -dx / norm * 25
FINISH_LINE_START = (ORIGIN[0] - gate_x / LON_METRES, ORIGIN[1] - gate_y / 111320)
FINISH_LINE_END = (ORIGIN[0] + gate_x / LON_METRES, ORIGIN[1] + gate_y / 111320)

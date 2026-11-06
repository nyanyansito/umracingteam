"""Deployment settings shared by every telemetry process.

Set UMR_CONFIG to a JSON file; UMR_MQTT_HOST/PORT override its broker settings.
The bundled track is a simulator default, not a hardware calibration.
"""
import json
import math
import os
from pathlib import Path

from simulated_track import COORDINATES, DISTANCES, FINISH_LINE_START, FINISH_LINE_END, TRACK_LENGTH


def load_config(path=None):
    path = path or os.environ.get("UMR_CONFIG")
    data = {} if not path else json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Telemetry configuration must be a JSON object")
    return data


CONFIG = load_config()
BROKER = os.environ.get("UMR_MQTT_HOST", CONFIG.get("broker", {}).get("host", "localhost"))
PORT = int(os.environ.get("UMR_MQTT_PORT", CONFIG.get("broker", {}).get("port", 1883)))
if not isinstance(BROKER, str) or not BROKER or not 1 <= PORT <= 65535:
    raise ValueError("Invalid MQTT host or port")

TRACK = CONFIG.get("track", {})
if not isinstance(TRACK, dict):
    raise ValueError("track must be an object")
DEFAULT_CHECKPOINTS = [list(COORDINATES[min(range(len(DISTANCES)), key=lambda i: abs(DISTANCES[i] - TRACK_LENGTH * f))]) + [60]
                       for f in (.25, .5, .75)]
TRACK_ID = TRACK.get("id", "red_bull_ring")
FINISH_START = tuple(TRACK.get("finish_start", FINISH_LINE_START))
FINISH_END = tuple(TRACK.get("finish_end", FINISH_LINE_END))
MINIMUM_LAP_SECONDS = TRACK.get("minimum_lap_seconds", 20)
MINIMUM_LAP_DISTANCE = TRACK.get("minimum_lap_distance_m", TRACK_LENGTH * .8)
MINIMUM_DEPARTURE = TRACK.get("minimum_departure_m", 50)
CHECKPOINTS = TRACK.get("checkpoints", DEFAULT_CHECKPOINTS)
STATE_FILE = Path(os.environ.get("UMR_STATE_FILE", CONFIG.get("state_file", "logs/lap-session.json")))
DASHBOARD_HOST = os.environ.get("UMR_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("UMR_DASHBOARD_PORT", "8050"))
STM32_HOST = os.environ.get("UMR_STM32_HOST", "localhost")
STM32_PORT = int(os.environ.get("UMR_STM32_PORT", "9000"))


def validate_track():
    for position in (FINISH_START, FINISH_END):
        if len(position) != 2 or not all(type(v) in (int, float) and math.isfinite(v) for v in position):
            raise ValueError("Finish endpoints must be [longitude, latitude]")
        if not -180 <= position[0] <= 180 or not -90 <= position[1] <= 90:
            raise ValueError("Finish coordinates are out of range")
    if FINISH_START == FINISH_END:
        raise ValueError("Finish endpoints must differ")
    for value in (MINIMUM_LAP_SECONDS, MINIMUM_LAP_DISTANCE, MINIMUM_DEPARTURE):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Lap eligibility thresholds must be positive finite numbers")
    for gate in CHECKPOINTS:
        if (not isinstance(gate, list) or len(gate) != 3
                or not all(type(v) in (int, float) and math.isfinite(v) for v in gate)
                or not -180 <= gate[0] <= 180 or not -90 <= gate[1] <= 90 or gate[2] <= 0):
            raise ValueError("Each checkpoint must be [longitude, latitude, radius_metres]")
    if TRACK and TRACK_ID != "red_bull_ring" and not all(k in TRACK for k in (
            "finish_start", "finish_end", "minimum_lap_distance_m", "checkpoints")):
        raise ValueError("A custom track requires its finish line, minimum distance and ordered checkpoints")


validate_track()

"""Atomic persistence of completed laps; partial laps always re-arm after restart."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def track_signature(analysis):
    timer = analysis.timer
    return hashlib.sha256(json.dumps([timer.finish_start, timer.finish_end, timer.checkpoints,
                                      timer.minimum_lap_seconds, timer.minimum_distance_m,
                                      timer.minimum_departure_m], sort_keys=True).encode()).hexdigest()


def save_session(path, analysis):
    data = {"version": 1, "track": track_signature(analysis), "session_id": analysis.session_id,
            "history": list(analysis.history), "laps": analysis.laps,
            "best_time": analysis.best_time, "best_lap": analysis.best_lap,
            "best_top_speed": analysis.best_top_speed, "session_top_speed": analysis.session_top_speed,
            "selected_reference": analysis.selected_reference, "completed_count": analysis.completed_count,
            "lap_number": analysis.timer.lap_number, "track_id": analysis.track_id,
            "order": {k: getattr(analysis.timer.order, k) for k in
                      ("session", "timestamp", "sequence", "unwrapped_ms", "generation")}}
    encoded = json.dumps(data, allow_nan=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            temporary = out.name
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def restore_session(path, analysis):
    path = Path(path)
    if not path.exists():
        return False
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError("Saved session is too large")
    def reject_constant(value):
        raise ValueError(f"Invalid saved number: {value}")
    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    if data.get("version") != 1 or data.get("track") != track_signature(analysis):
        return False
    laps = {int(n): lap for n, lap in data["laps"].items()}
    if len(laps) > 52 or len(data["history"]) > 50:
        raise ValueError("Saved session exceeds lap limits")
    from telemetry_validation import FAST_FIELDS, is_number
    for n, lap in laps.items():
        if n <= 0 or not is_number(lap["seconds"]) or not 0 < lap["seconds"] <= 600:
            raise ValueError("Invalid saved lap")
        if not 2 <= len(lap["points"]) <= 10001 or not len(lap["points"]) == len(lap["inputs"]) == len(lap["channels"]):
            raise ValueError("Invalid saved traces")
        previous_time = previous_distance = -1
        for point, channel in zip(lap["points"], lap["channels"]):
            if (len(point) != 4 or not all(is_number(v) for v in point)
                    or point[2] < previous_time or point[3] < previous_distance
                    or not isinstance(channel, dict)
                    or any(channel.get(k) is not None and not is_number(channel[k]) for k in FAST_FIELDS)):
                raise ValueError("Invalid saved telemetry")
            previous_time, previous_distance = point[2:]
    if data["best_lap"] is not None and data["best_lap"] not in laps:
        raise ValueError("Saved reference is missing")
    if data["selected_reference"] is not None and data["selected_reference"] not in laps:
        raise ValueError("Selected reference is missing")
    for row in data["history"]:
        if not isinstance(row, dict) or type(row.get("lap_number")) is not int or not is_number(row.get("lap_time_seconds")):
            raise ValueError("Invalid lap summary")
    for key in ("lap_number", "completed_count"):
        if type(data[key]) is not int or data[key] < 0:
            raise ValueError("Invalid lap count")
    order = data["order"]
    from telemetry_validation import is_uint
    if ((order["timestamp"] is not None and not is_uint(order["timestamp"], 32))
            or (order["sequence"] is not None and not is_uint(order["sequence"], 16))
            or (order["session"] is not None and not isinstance(order["session"], str))):
        raise ValueError("Invalid source identity")
    analysis.interrupt()
    analysis.laps = laps
    analysis.history.extend(data["history"])
    for key in ("session_id", "best_time", "best_lap", "best_top_speed", "session_top_speed",
                "selected_reference", "completed_count", "track_id"):
        setattr(analysis, key, data[key])
    analysis.timer.lap_number = data["lap_number"]
    analysis.timer.session_id = analysis.session_id
    for key, value in order.items():
        if key in ("session", "timestamp", "sequence", "unwrapped_ms", "generation"):
            setattr(analysis.timer.order, key, value)
    analysis._set_reference(analysis.selected_reference or analysis.best_lap)
    analysis.revision += 1
    return True

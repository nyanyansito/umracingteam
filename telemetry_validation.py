"""Shared wire-value checks; numeric range violations remain visible."""
import json
import math

FAST_FIELDS = ("throttle_percent", "brake_bar", "steering_degrees", "speed_kmh")


def is_number(value):
    # bool is a subclass of int, but is not a sensor measurement.
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def is_uint(value, bits):
    return type(value) is int and 0 <= value < (1 << bits)


def decode_object(payload):
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Telemetry must be a JSON object")
    return value


def validate_fast_sample(sample):
    if not isinstance(sample, dict):
        raise ValueError("Telemetry must be an object")
    if not all(is_number(sample.get(field)) for field in FAST_FIELDS):
        raise ValueError("Fast telemetry requires four finite numeric channels")
    if not is_uint(sample.get("sequence"), 16):
        raise ValueError("Invalid sequence")
    if not is_uint(sample.get("timestamp_ms"), 32):
        raise ValueError("Invalid timestamp")
    if "session_id" in sample and not isinstance(sample["session_id"], str):
        raise ValueError("Invalid session identifier")
    return sample


def json_safe(value):
    """Keep invalid values diagnosable while producing standard JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value

"""Data processing / validation layer for the UM F1 Pit-Wall telemetry pipeline.

Role in the pipeline
---------------------
STM32 simulator -> UART -> ESP32 gateway -> MQTT (raw topics) -> THIS MODULE -> MQTT
(processed topic) -> dashboard / logger / lap timer

The ESP32 gateway (esp32_simulator.py) already converts the binary UART
payload into engineering units before it ever reaches MQTT (see
telemetry_packet.py: throttle_percent, brake_bar, steering_degrees,
speed_kmh). So this module's job is NOT to redo that scaling -- it is to:

  1. Provide reusable raw-count -> engineering-unit conversion functions
     (kept here so they can be reused/tested independently, and so any
     future channel that arrives as a raw count, e.g. wheel speeds or
     RPM, has a ready-made conversion path).
  2. Subscribe to every "implemented" raw telemetry topic, validate each
     field against the min/max range defined in channellist.csv, and
     republish a standardized "processed" message.
  3. Flag out-of-range values without discarding or clamping them --
     per team decision, the pipeline should never silently hide a bad
     reading; downstream tools decide what to do with the flag.

Range handling policy: FLAG, DO NOT MODIFY.
    If a value falls outside [min_value, max_value] from
    channellist.csv, it is marked "out_of_range" in the processed
    message but the value itself is passed through unchanged. This
    preserves raw-vs-processed traceability for the Week 3 accuracy
    report -- an out-of-range flag should never be the reason a value
    looks "wrong" when compared back to the source.

Reading channellist.csv at runtime
------------------------------------
CHANNEL_SPECS is no longer typed in by hand. On import, this module
opens channellist.csv (expected in the same folder as this file --
i.e. the repo root) and reads min_value/max_value/unit straight from
it. Editing the CSV and re-running the pipeline is now enough to
change validation ranges; nothing here needs to be touched for that.

The one thing that still can't come from the CSV automatically: the
CSV describes channels with a human-readable `channel_name`
(e.g. "throttle_position", "gps_speed", "lap_time"), but the ACTUAL
JSON keys published on the wire by the simulators are different in
several cases (e.g. "throttle_percent", "gps_speed_kmh",
"lap_time_seconds" -- see telemetry_packet.py, gps_simulator.py,
lap_timer.py). WIRE_FIELD_TO_CSV_CHANNEL_NAME below is the one mapping
that still has to be maintained by hand, since it connects "what the
code receives" to "what the CSV calls it". If a teammate renames a
channel_name in the CSV, or a wire field name changes in a simulator,
update this mapping to match.

Only channels marked status=implemented in channellist.csv are
loaded, because those are the only ones any simulator or gateway
script actually publishes. Rows marked status=planned (wheel speeds,
RPM, longitudinal/lateral acceleration) are skipped automatically --
nothing publishes them yet, so there is nothing on the wire to
validate. Once a simulator starts publishing one, add its wire field
name to WIRE_FIELD_TO_CSV_CHANNEL_NAME (and to TOPIC_FIELDS below) and
it will start being picked up from the CSV with no other changes.

Status topics
-------------
Two different scripts publish status messages, to two different topics:
  - esp32_simulator.py publishes the REAL gateway status (online plus the
    counters packet_count, decode_error_count, dropped_count) to
    umf1/car01/gateway/status.
  - mqtt_publisher.py is a small manual test script that publishes just
    {"online": true} to umf1/car01/status.
Both topics are validated here (TOPIC_FIELDS below). The CSV's mqtt_topic
column for the four status channels points at gateway/status because
that is where the real data comes from.

Limits in the processed message
-------------------------------
Every processed message also carries a "limits" entry ({field: {"min":
.., "max": ..}}) for each validated field, so a display (the dashboard)
can say *why* a value was flagged, e.g. "300 is outside 0 to 100",
without having to read the CSV itself.
"""

import csv
import json
from pathlib import Path

import paho.mqtt.client as mqtt
from mqtt_runtime import run_mqtt
from telemetry_validation import decode_object, is_number, is_uint, json_safe


BROKER = "localhost"
PORT = 1883

# Raw topics this module subscribes to.
FAST_TOPIC = "umf1/car01/telemetry/fast"
GPS_TOPIC = "umf1/car01/telemetry/gps"
LAP_TOPIC = "umf1/car01/telemetry/lap"
STATUS_TOPIC = "umf1/car01/status"  # manual test publisher (mqtt_publisher.py)
GATEWAY_STATUS_TOPIC = "umf1/car01/gateway/status"  # real ESP32 gateway status

# Each source topic gets its own processed topic (source topic +
# "/processed"), rather than one shared topic for everything.
#
# Why: mqtt_subscriber.py tracks packet sequence numbers PER TOPIC,
# assuming one continuous counter per topic. lap/status messages don't
# carry a sequence number at all, and fast/gps use unrelated counters
# -- merging them onto one topic either crashes the subscriber's
# sequence-gap checker (math on a missing/None sequence) or floods it
# with false "missed packets" warnings from interleaving two counters.
# Giving each source its own processed topic keeps that checker
# working correctly with no changes needed to mqtt_subscriber.py.
PROCESSED_TOPIC_SUFFIX = "/processed"

RANGE_OK = "ok"
RANGE_OUT_OF_RANGE = "out_of_range"
RANGE_INVALID = "invalid"
RANGE_MISSING = "missing"

# The channel sheet is expected to sit next to this file, in the repo
# root, alongside dashboard.py / esp32_simulator.py / etc. The team has
# used both "channellist.csv" and "channel-list.csv" as its name, so
# either one is accepted (the first that exists wins) -- a file-name
# mix-up should never stop the pipeline.
CSV_FILENAME_CANDIDATES = ("channellist.csv", "channel-list.csv")
_REPO_ROOT = Path(__file__).resolve().parent
CSV_PATH = next(
    (
        _REPO_ROOT / name
        for name in CSV_FILENAME_CANDIDATES
        if (_REPO_ROOT / name).exists()
    ),
    _REPO_ROOT / CSV_FILENAME_CANDIDATES[0],
)
CSV_FILENAME = CSV_PATH.name


# ---------------------------------------------------------------------
# Wire field name -> channellist.csv channel_name
# (see "Reading channellist.csv at runtime" above for why this exists)
# ---------------------------------------------------------------------
WIRE_FIELD_TO_CSV_CHANNEL_NAME = {
    "throttle_percent": "throttle_position",
    "brake_bar": "brake_pressure",
    "steering_degrees": "steering_angle",
    "speed_kmh": "vehicle_speed",
    "latitude": "latitude",
    "longitude": "longitude",
    "gps_speed_kmh": "gps_speed",
    "gps_valid": "gps_valid",
    "lap_number": "lap_number",
    "lap_time_seconds": "lap_time",
    "online": "online",
    "packet_count": "packet_count",
    "decode_error_count": "decode_error_count",
    "dropped_count": "dropped_count",
}

# Raw-count -> engineering-unit scale factors, from telemetry_packet.py.
# Not something channellist.csv tracks -- only the four fast-telemetry
# fields still travel as fixed-point counts over UART before the ESP32
# converts them, so only they need a scale factor here.
SCALE_FACTORS = {
    "throttle_percent": 100,
    "brake_bar": 100,
    "steering_degrees": 10,
    "speed_kmh": 100,
}

# Which channel fields are expected in the payload of each raw topic.
# timestamp_ms and sequence (csv rows 9-10) are treated as packet
# metadata rather than validated channels -- every payload carries
# them, but they describe the packet, not a sensor reading.
#
# online / packet_count / decode_error_count / dropped_count are all
# listed under STATUS_TOPIC because esp32_simulator.py publishes them
# together in one message to that one topic (see module docstring).
TOPIC_FIELDS = {
    FAST_TOPIC: ["throttle_percent", "brake_bar", "steering_degrees", "speed_kmh"],
    GPS_TOPIC: ["latitude", "longitude", "gps_speed_kmh", "gps_valid"],
    LAP_TOPIC: ["lap_number", "lap_time_seconds"],
    STATUS_TOPIC: ["online"],
    GATEWAY_STATUS_TOPIC: [
        "online",
        "packet_count",
        "decode_error_count",
        "dropped_count",
    ],
}


# ---------------------------------------------------------------------
# Loading channel specs from channellist.csv
# ---------------------------------------------------------------------

def _parse_number(text):
    """Convert a CSV cell to int/float, or None if it's blank.

    channellist.csv leaves max_value blank for unbounded counters
    (packet_count, decode_error_count, dropped_count) -- blank must
    become None, not an error or a zero.
    """
    if text is None or text.strip() == "":
        return None
    try:
        return int(text)
    except ValueError:
        return float(text)


def load_channel_rows(csv_path=CSV_PATH):
    """Read channellist.csv into a dict keyed by channel_name."""
    rows_by_channel_name = {}

    with open(csv_path, newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)

        for row in reader:
            rows_by_channel_name[row["channel_name"]] = {
                "channel_id": _parse_number(row.get("channel_id")),
                "unit": row.get("unit"),
                "min_value": _parse_number(row.get("min_value")),
                "max_value": _parse_number(row.get("max_value")),
                "status": row.get("status"),
            }

    return rows_by_channel_name


def build_channel_specs(csv_path=CSV_PATH):
    """Build CHANNEL_SPECS (wire field name -> spec) directly from
    channellist.csv. This is what makes CSV edits take effect without
    any code change: min_value/max_value/unit always come straight
    from the file on disk at import time.

    Raises a clear error if the CSV is missing a channel this module
    expects, so a rename in the CSV fails loudly here instead of
    silently skipping validation for that field.
    """
    csv_rows = load_channel_rows(csv_path)
    specs = {}

    for wire_field, csv_channel_name in WIRE_FIELD_TO_CSV_CHANNEL_NAME.items():
        row = csv_rows.get(csv_channel_name)

        if row is None:
            raise KeyError(
                f"channel_name '{csv_channel_name}' (expected for wire "
                f"field '{wire_field}') was not found in {csv_path}. "
                f"Check that WIRE_FIELD_TO_CSV_CHANNEL_NAME still matches "
                f"channellist.csv."
            )

        if row["status"] != "implemented":
            continue  # planned channels aren't published anywhere yet

        specs[wire_field] = {
            "csv_channel_id": row["channel_id"],
            "csv_channel_name": csv_channel_name,
            "unit": row["unit"],
            "min_value": row["min_value"],
            "max_value": row["max_value"],
            "scale": SCALE_FACTORS.get(wire_field),
        }

    return specs


try:
    CHANNEL_SPECS = build_channel_specs()
except FileNotFoundError:
    raise FileNotFoundError(
        f"Could not find the channel sheet. Looked for "
        f"{' or '.join(CSV_FILENAME_CANDIDATES)} in {_REPO_ROOT}. "
        f"data_processing.py expects channellist.csv in the same "
        f"folder as this script (the repo root)."
    )


# ---------------------------------------------------------------------
# Engineering-unit conversion
# ---------------------------------------------------------------------

def convert_raw_to_engineering(raw_value, scale_factor):
    """Convert a raw fixed-point integer count into engineering units.

    This mirrors the scaling defined in telemetry-packet.md, e.g. a
    raw throttle count of 4250 with scale_factor=100 becomes 42.50%.

    Provided for channels that arrive as raw counts (currently none of
    the implemented MQTT topics do -- the ESP32 gateway already
    converts fast-telemetry fields -- but this keeps a tested, reusable
    path ready for any channel that is added upstream of that
    conversion, or for verifying the gateway's own conversion offline).
    """
    if scale_factor in (None, 0):
        raise ValueError("scale_factor must be a non-zero number")

    return raw_value / scale_factor


# ---------------------------------------------------------------------
# Range validation
# ---------------------------------------------------------------------

def validate_range(value, min_value, max_value):
    """Return True if value falls within [min_value, max_value].

    Either bound may be None (channellist.csv leaves max_value blank
    for unbounded counters like packet_count) -- a None bound is
    treated as "no limit on that side" rather than failing the check.
    """
    if not is_number(value):
        return False
    if min_value is not None and value < min_value:
        return False

    if max_value is not None and value > max_value:
        return False

    return True


def process_fields(payload, field_names):
    """Validate a set of fields from a raw MQTT payload against
    CHANNEL_SPECS.

    Returns (processed_values, range_flags):
      - processed_values: field -> value, UNCHANGED from the input,
        even for out-of-range readings (flag-only policy).
      - range_flags: field -> "ok" | "out_of_range"

    Missing and invalid fields are flagged explicitly. Planned channels
    without a specification are skipped. Finite out-of-range readings
    remain unchanged.
    """
    processed_values = {}
    range_flags = {}

    if not isinstance(payload, dict):
        raise ValueError("Telemetry must be a JSON object")
    for field_name in field_names:
        if field_name not in CHANNEL_SPECS:
            continue

        if field_name not in payload:
            range_flags[field_name] = RANGE_MISSING
            continue

        value = payload[field_name]
        spec = CHANNEL_SPECS[field_name]

        if field_name in ("online", "gps_valid"):
            valid_type = type(value) is bool
        else:
            valid_type = is_number(value)
            if field_name in ("lap_number", "packet_count", "decode_error_count", "dropped_count"):
                valid_type = type(value) is int and is_number(value)

        if not valid_type:
            processed_values[field_name] = json_safe(value)
            range_flags[field_name] = RANGE_INVALID
            continue

        in_range = validate_range(int(value) if type(value) is bool else value,
                                  spec["min_value"], spec["max_value"])

        processed_values[field_name] = value
        range_flags[field_name] = RANGE_OK if in_range else RANGE_OUT_OF_RANGE

    return processed_values, range_flags


def build_processed_message(topic, payload):
    """Build the standardized processed-data message for one raw
    telemetry payload.

    Returns None if the topic isn't one this module handles.
    """
    field_names = TOPIC_FIELDS.get(topic)

    if field_names is None:
        return None

    processed_values, range_flags = process_fields(payload, field_names)

    for field, bits in (("sequence", 16), ("timestamp_ms", 32)):
        if field in payload and not is_uint(payload[field], bits):
            range_flags[field] = RANGE_INVALID
        elif field not in payload and topic in (FAST_TOPIC, GPS_TOPIC):
            range_flags[field] = RANGE_MISSING

    message = {
        "source_topic": topic,
        "values": processed_values,
        "range_flags": range_flags,
        "any_out_of_range": any(
            flag != RANGE_OK for flag in range_flags.values()
        ),
        "any_invalid_data": any(
            flag in (RANGE_INVALID, RANGE_MISSING) for flag in range_flags.values()
        ),
        "limits": {
            field_name: {
                "min": CHANNEL_SPECS[field_name]["min_value"],
                "max": CHANNEL_SPECS[field_name]["max_value"],
            }
            for field_name in processed_values
        },
    }

    # Only included when the source payload actually has them, so a
    # lap/status message (which never carries a sequence number) never
    # ends up publishing "sequence": None downstream.
    if is_uint(payload.get("sequence"), 16):
        message["sequence"] = payload["sequence"]

    if is_uint(payload.get("timestamp_ms"), 32):
        message["timestamp_ms"] = payload["timestamp_ms"]

    if isinstance(payload.get("session_id"), str):
        message["session_id"] = payload["session_id"]

    return message


# ---------------------------------------------------------------------
# Raw-vs-processed verification (used by unit tests and by the Week 3
# end-to-end accuracy report)
# ---------------------------------------------------------------------

def verify_passthrough(raw_payload, processed_message):
    """Confirm that every value in the processed message exactly
    matches the corresponding raw payload value.

    For finite numeric readings this should always be True. Invalid
    nonfinite numbers are represented as null in processed JSON; the
    lossless session recording preserves their original wire payload.
    """
    for field_name, processed_value in processed_message["values"].items():
        if raw_payload.get(field_name) != processed_value:
            return False

    return True


# ---------------------------------------------------------------------
# MQTT wiring
# ---------------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Data processor connected to Mosquitto")
        for topic in TOPIC_FIELDS:
            print(f"Listening to {topic}")
            client.subscribe(topic)
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    try:
        payload = decode_object(message.payload)
    except (ValueError, UnicodeDecodeError):
        print("Data processor received an invalid message")
        return

    processed_message = build_processed_message(message.topic, payload)

    if processed_message is None:
        return

    client.publish(
        message.topic + PROCESSED_TOPIC_SUFFIX,
        json.dumps(processed_message, allow_nan=False),
        qos=0,
    )

    if processed_message["any_out_of_range"]:
        flagged = {
            field: flag
            for field, flag in processed_message["range_flags"].items()
            if flag != RANGE_OK
        }
        print(
            f"WARNING: out-of-range value(s) on {message.topic}: {flagged}"
        )


if __name__ == "__main__":
    print(f"Loaded {len(CHANNEL_SPECS)} implemented channel(s) from {CSV_PATH}")

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="umf1-data-processor",
    )

    client.on_connect = on_connect
    client.on_message = on_message

    run_mqtt(client, BROKER, PORT)

# Pit Wall Telemetry System

The current development pipeline is:

Simulated STM32 → binary TCP byte stream standing in for UART → simulated
ESP32 → MQTT → validation, recording and live dashboard.
GPS is currently a separate MQTT simulator feeding the lap timer.

## Setup

Use Python 3.13 or newer and install the Python dependencies:

```sh
python -m venv .venv
# Activate the environment for your operating system, then:
python -m pip install -r requirements.txt
```

Install Mosquitto separately. MQTT Explorer is optional for inspecting traffic.
Run each program below in its own terminal, from the repository root:

```text
mosquitto
python stm32_link_server.py
python esp32_simulator.py
python gps_simulator.py
python lap_timer.py
python telemetry_logger.py
python data_processing.py
python dashboard.py
```

Open http://127.0.0.1:8050. Optionally run `python mqtt_subscriber.py` to
inspect traffic and packet-loss diagnostics. Stop processes with Ctrl+C.
Core telemetry clients retry when Mosquitto is initially unavailable or
restarts, so starting the broker later does not require restarting them.
The standalone manual publisher and capture demo are diagnostic tools.

## Validation and connection health

The processor preserves finite readings, including numeric range violations.
Each expected channel has a flag: `ok`, `out_of_range`, `invalid`, or `missing`.
Wrong types, nulls and nonfinite numbers cannot be certified healthy.
Nonfinite values are represented as null in processed JSON; recordings retain
original payload bytes. `any_invalid_data` identifies malformed/incomplete
samples; `any_out_of_range` remains the compatibility summary for any bad flag.
Invalid sequence/timestamp metadata is flagged and omitted from processed output.

The dashboard updates all history channels together only after checking a
complete sample. It keeps 1,000 samples (about ten seconds at the 100 Hz target).
Raw data and validation expire after two seconds without updates. Gateway
status expires after three seconds. An alert timing out cannot turn stale data
green. Metric values remain visible as the last known values under the status
warning. Disconnects explicitly invalidate freshness.

Gateway `online` describes the gateway process/MQTT connection;
`stm32_connected`, `telemetry_fresh`, and `last_valid_packet_age_ms` describe
the vehicle-side link separately. Heartbeats continue during STM32 retries.
The broker's retained offline last will reports gateway connection loss.
`packet_count` counts telemetry accepted by the local MQTT client, not confirmed
pit-wall delivery (fast telemetry uses QoS 0).

## Lap timing

The first valid directional finish-line crossing establishes a baseline;
the next eligible crossing completes the first lap. Crossing times are
interpolated between fixes. Invalid fixes, gaps over one second in source or
receive time, implausible position jumps, and source resets discard continuity.
Timing re-arms at the next crossing; incomplete laps are not published.
The current finish line and minimum lap time are for the circular simulator.
Set `FINISH_LINE_START`, `FINISH_LINE_END` and `MINIMUM_LAP_TIME_SECONDS` in
`lap_timer.py` for the actual track. The direction is from the negative side
to the nonnegative side of the directed start-to-end line.

## Recording and replay

The logger creates matching files under `logs/`:

- `telemetry_<session>.csv`: complete valid fast samples, with receive time
  and source session ID, compatible with earlier fast CSV replay.
- `telemetry_<session>.jsonl`: every `umf1/car01/#` topic, including GPS,
  laps, gateway status and validation flags. Each record contains host wall
  and monotonic receive timestamps, topic, QoS, retained flag, exact payload
  bytes in base64, and a JSON-safe decoded view when available.

```sh
python replay_telemetry.py logs/telemetry_<session>.csv --speed 1
python replay_telemetry.py logs/telemetry_<session>.jsonl --speed 1
```

JSONL replay uses host monotonic timing. By default it skips recorded lap and
processed topics so running processors can regenerate them. `--all-topics`
also replays those derived messages; stop their live producers first to avoid
duplicates. Replay sends no retained messages. Use an isolated test broker and
stop live source simulators when replaying a recording.

## Tests and hardware boundary

```sh
python -m unittest discover -v
```

Regression tests cover malformed input, stale dashboard data, atomic updates,
GPS outages and first-lap timing, counter resets/rollover, gateway retry
heartbeats, recording/replay, framing recovery, and a fixed firmware wire vector.
See `telemetry-packet.md` for the finalized CRC and exact packet bytes.

Fast simulation now targets 100 Hz; this does not establish physical UART or
Wi-Fi performance. The repository still has no STM32/ESP32 firmware or physical
serial backend. GPIO assignments, sensor calibration and GPS/status/event binary
payloads require hardware implementation and bench verification. Keep MQTT
brokers and addresses configured for the intended test network; all Python
programs currently default to localhost.

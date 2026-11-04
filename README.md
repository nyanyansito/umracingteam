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

### Start everything with one command

Activate your virtual environment, then run:

```sh
python run_telemetry.py
```

This starts the STM32 link simulator, ESP32 gateway, GPS simulator, lap timer,
logger, processor and dashboard using the same Python interpreter. Open
http://127.0.0.1:8050. Press Ctrl+C in the launcher terminal to stop them together.
Close any individually running copies of these scripts first.

The launcher reuses a broker already listening on localhost:1883, including a
Mosquitto Windows service. Otherwise it starts Mosquitto from PATH or its normal
Windows installation folder. It only stops the broker if it started that broker.
If you installed Mosquitto elsewhere, use PowerShell:

```powershell
python run_telemetry.py --mosquitto-path "C:\Program Files\mosquitto\mosquitto.exe"
```

Each process writes a separate log under `logs/launcher_<session>/`. If a program
exits unexpectedly, the launcher reports its log file and stops the other owned
processes. Add `--subscriber` to include packet diagnostics. Replay, fake-data
injection and standalone print demos are intentionally separate commands.

### Start programs individually

For debugging, run each program below in its own terminal, from the repository root:

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
interpolated between fixes. Invalid fixes, gaps over one second in GPS source
timestamps, implausible position jumps, and source resets discard continuity.
Late delivery of otherwise consecutive source samples does not reset the lap.
Timing re-arms at the next crossing; incomplete laps are not published.
The current finish line is for the Red Bull Ring simulator.
Set `FINISH_LINE_START`, `FINISH_LINE_END` and `MINIMUM_LAP_TIME_SECONDS` in
`lap_timer.py` for the actual track. The direction is from the negative side
to the nonnegative side of the directed start-to-end line.

## Live lap dashboard

The dashboard now includes current lap number and elapsed time, last/best lap,
live delta, a GPS track trace with the current position, a completed-lap table,
and a delta-versus-reference-distance graph. The trace uses local east/north
coordinates in metres, so it works offline without map tiles or an API key.
The first full uninterrupted lap captured by the dashboard becomes the reference;
a faster complete lap replaces it automatically. Negative delta means faster,
positive means slower. The stacked list shows lap number, time, GPS top speed
and delta against the current session best. All row deltas update when a new
best is set. A session-best row and BEST badge identify the fastest lap.
The session top-speed card includes valid out-lap samples as well.

Lap calculations run in the dashboard using the same `LapTimer` implementation
as the standalone lap publisher; the dashboard does not need the standalone
publisher to display lap timing. It must receive live GPS messages and observe
two directional finish crossings before it can record its first complete lap.
The simulator follows a bundled Red Bull Ring centreline in Spielberg, Austria.
It starts just before the finish: timing arms after about one second, with the
first complete reference after roughly 2.5 minutes. The full circuit outline
appears as soon as simulator GPS arrives. Speeds vary through corners and
straights, and successive laps vary by up to two percent. Fast telemetry and
GPS use the same synthetic driving profile at a given elapsed time, but their
independent process start times can offset their phases. This is a club-car
test profile within the existing 120 km/h sensor limits, not F1 physics.
Circuit data: https://github.com/bacinger/f1-circuits/blob/master/circuits/at-1969.geojson
(MIT licence and attribution included in `tracks/LICENSE.md`).

Delta is calculated by projecting the current GPS position onto nearby forward
segments of the reference and interpolating the reference's elapsed time there.
It is unavailable before a complete reference exists or when the position is
more than 20 m from a plausible reference segment. GPS source gaps over one second,
invalid fixes and broker disconnects invalidate the current lap. Completed laps
remain available across GPS dropouts, but a source restart clears the session.
The dashboard keeps up to 50 completed-lap results in memory; restart/reload of
the Python dashboard clears this analysis state. A browser refresh does not.

A stale display freezes the last known lap time and hides delta; refreshing the
page never clears an active lap. Chart refresh rates are 4 Hz for the lap panel
and 2 Hz for sensor histories, independently of data reception. The simulators
use a paced virtual clock: host scheduling pauses freeze simulated time instead
of teleporting the car forward or emitting a catch-up flood of packets. On a slow
machine simulated lap duration can therefore exceed its nominal wall-clock time.

The default finish line is for the simulator: configure `lap_timer.py` for the
actual circuit. The delta is a GPS estimate, not a transponder timing result.
Sensor graphs still use their own telemetry timestamps; they are not overlaid
onto GPS positions because the current simulators have independent clocks.

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

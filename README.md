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
complete, advancing sample. Duplicates and late packets do not refresh freshness.
Validation must match the displayed sample's session, sequence and timestamp before
it can show a healthy result. It keeps 1,000 samples (about ten seconds at the 100 Hz target).
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

## Lap timing and live analysis

`python lap_timer.py` starts the single authoritative lap service. It owns lap
numbering, current lap time, best/reference laps, GPS continuity and completed-lap
storage. The dashboard subscribes to this service; it no longer starts a separate
lap counter. A viewer can join halfway through a session and see the same lap
numbers and reference as the logger and other viewers.

A lap begins at a directional finish-line crossing. A completed lap must also
meet the minimum duration, minimum travelled distance, minimum departure from the
finish, and all ordered checkpoints. The bundled Red Bull Ring configuration uses
three checkpoints and an 80 percent circuit-distance minimum. This prevents GPS
jitter at the finish from creating a stationary best lap and rejects skipped
checkpoints. Rejected crossings re-arm timing and display a reason; they cannot
become a reference. Out-laps and interrupted laps are not eligible.

Invalid GPS fixes, source gaps over one second, implausible jumps and broker
disconnects discard the partial lap. Completed laps survive interruptions.
Duplicate and late packets are ignored. A new, valid source session ID starts a
new session; delayed packets from retired sessions cannot switch it back. Legacy
sources without a session ID must reset their sequence to zero on reboot.
Counter rollover is unwrapped instead of looking like a restart.

Delta projects the current GPS fix onto the reference and compares elapsed times
at that distance. Negative is faster. Matching can reacquire after an excursion,
but leaves delta unavailable when candidate sections are ambiguous or farther
than 20 metres away. Missing comparisons create gaps in the delta graph.

## Dashboard controls

The dashboard provides:

- Current, last and best lap times, live delta, GPS top speeds and a lap table.
- A GPS track trace with distinct current/reference lines. Colour the current
  trace by pedal activity, speed, throttle or brake pressure.
- Full-lap throttle, brake, steering and speed plots, aligned to reference
  distance, with reference traces underneath the live lap.
- A rolling 1,000-sample sensor view (about 10 seconds at 100 Hz), updated at
  10 Hz using incremental appends. Sequence gaps and restarts break/reset traces.
  Display limits come from the channel sheet and expand to keep excursions visible.
- Linked inspection: hover a lap-input or delta graph to mark that position on
  the map; hover a mapped segment to place a shared cursor on the plots.
  Zoom survives updates. **Return to live** clears inspection and restores the view.
- A reference selector: session best automatically, or any stored completed lap.
  Selection is shared by viewers of this car/session. Selecting another reference
  never changes the session-best time or the table's deltas against that best.

The lap plots and map refresh at up to 5 Hz. Each refresh waits for the
previous request to finish, so slow browsers/networks cannot create an endless
queue of superseded results. The clock receives anchors at 10 Hz and
animates locally, extrapolating at most 150 ms beyond the latest GPS fix.
Millisecond digits are an interpolation estimate, not millisecond GPS accuracy.
A stale display freezes the clock and hides live delta.

The lap service publishes compact append updates at 10 Hz and a retained full
snapshot every two seconds, at lap boundaries, and when requested by a viewer.
A missing update causes that viewer to request a snapshot and pause its live
indication until recovered. A retained snapshot alone never proves liveness.
The service measures GPS age from arrival time and observed source-clock progress,
so a delayed backlog is not presented as current. Initial absolute one-way latency
cannot be determined without clock synchronisation between the car and pit wall.

## Input synchronisation and simulation

Use `python run_telemetry.py` for simulation. The launcher gives the STM32 and GPS
simulators the same monotonic clock origin and session ID. Their samples therefore
refer to the same car position even when the processes start at different times.
Host stalls skip source samples rather than independently slowing the two clocks;
a real gap can invalidate a simulated lap. The isolated `SimulationClock` fixture
still supports a paced virtual clock when no shared origin is configured.

GPS simulator messages carry throttle, brake, steering and speed sampled at the
fix's position. The lap service also supports hardware GPS messages without these
channels: it can attach a fast sample within 150 ms **only when both messages have
the same session ID and acquisition timestamp domain**. Embedded GPS-synchronised
inputs take precedence. Missing or unsynchronised inputs remain unavailable.
Do not reuse a boot ID with a reset clock.

Full-lap input comparisons currently use the GPS-aligned samples (10 Hz in the
simulator). The rolling input graphs and recordings preserve the faster 100 Hz
stream; they should be used when inspecting events shorter than a GPS interval.
The map uses green for throttle, red for braking (priority when both apply), grey
for coasting and dark grey for unknown input. Brake over 1 bar and throttle over
5 percent define these modes. Numeric colour modes expose the actual magnitudes.

The simulator follows the bundled Red Bull Ring centreline in Austria, starts
just before the finish, and establishes a complete reference after approximately
2.5 minutes. It uses a club-car profile within the 120 km/h limits, not F1 physics.
Circuit data: https://github.com/bacinger/f1-circuits/blob/master/circuits/at-1969.geojson
(MIT licence and attribution in `tracks/LICENSE.md`).

## Persistence and deployment configuration

Completed lap summaries, GPS/input traces, best lap and reference selection are
saved atomically to `logs/lap-session.json`. Up to 50 recent completed laps are
kept, plus the best/selected reference if older. Restarting the lap service restores
these for the same source session and track; the partial lap must re-arm at the
next finish crossing. Browser refreshes do not affect timing. Raw recordings still
provide the full session history.

Copy `telemetry-config.example.json` and set `UMR_CONFIG` to its absolute path to
configure the broker, storage file, finish endpoints, eligibility thresholds and
ordered checkpoints. Coordinates are `[longitude, latitude]`; checkpoints include
a third value, their radius in metres. A custom track ID requires explicit finish
coordinates, minimum lap distance and checkpoints. Configure these for the real
circuit before using hardware.

Environment overrides are `UMR_MQTT_HOST`, `UMR_MQTT_PORT`, `UMR_STATE_FILE`,
`UMR_DASHBOARD_HOST`, `UMR_DASHBOARD_PORT`, `UMR_STM32_HOST` and `UMR_STM32_PORT`.
The default dashboard binds to `127.0.0.1:8050`. For a remote broker, the launcher
starts clients without attempting to start a local Mosquitto instance. MQTT topics
currently identify `umf1/car01`; deployment still needs the intended broker access
configuration. `UMR_SIM_START_SECONDS` and `UMR_SIM_SESSION_ID` are set by the
launcher for simulation and should not be injected into a hardware gateway.

## Recording and replay

The logger creates matching files under `logs/`:

- `telemetry_<session>.csv`: complete valid fast samples, with receive time
  and source session ID, compatible with earlier fast CSV replay.
- `telemetry_<session>.jsonl`: raw telemetry, GPS, completed laps, gateway
  status and validation flags from `umf1/car01/#`. Derived lap display snapshots,
  appends and commands are omitted by default because recorded GPS/inputs regenerate
  them. Set `UMR_LOG_LAP_STATE=1` to include these for protocol diagnostics; doing
  so substantially increases recording size. Each record contains host wall
  and monotonic receive timestamps, topic, QoS, retained flag, exact payload
  bytes in base64, and a JSON-safe decoded view when available.

```sh
python replay_telemetry.py logs/telemetry_<session>.csv --speed 1
python replay_telemetry.py logs/telemetry_<session>.jsonl --speed 1
```

JSONL replay uses host monotonic timing. By default it skips recorded lap events, lap snapshots/updates/commands and
processed topics so the running lap service and processor can regenerate them. `--all-topics`
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
payloads require hardware implementation and bench verification. Configure the intended MQTT broker and circuit with
`telemetry-config.example.json`.

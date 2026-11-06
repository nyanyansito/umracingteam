# MQTT Topic Specification

## Purpose

This document defines the MQTT topics used to send telemetry from the vehicle to the pit-wall computer.

## Data Flow

ESP32 → Mosquitto MQTT Broker → Python Subscriber → Graphical Software

- The ESP32 publishes telemetry.
- Mosquitto distributes the messages.
- The Python program subscribes to the messages.

## Topic List

| Topic | Update Rate | Purpose |
|---|---:|---|
| umf1/car01/telemetry/fast | 100 Hz | Throttle, brake, steering and speed |
| umf1/car01/telemetry/gps | 10 Hz | GPS position, speed and time |
| umf1/car01/telemetry/lap | When updated | Lap, sector and delta times |
| umf1/car01/status | 1 Hz | System and connection status |
| umf1/car01/events | When required | Faults and important events |

## Fast Telemetry Example

Topic:

```text
umf1/car01/telemetry/fast
```

Example message:

```json
{
  "sequence": 125,
  "timestamp_ms": 15240,
  "throttle_percent": 42.5,
  "brake_bar": 0.0,
  "steering_degrees": -18.5,
  "speed_kmh": 63.4
}
```

## GPS Example

Topic:

```text
umf1/car01/telemetry/gps
```

Example message:

```json
{
  "sequence": 152,
  "timestamp_ms": 15200,
  "latitude": 3.1215,
  "longitude": 101.6532,
  "gps_speed_kmh": 63.4,
  "gps_valid": true
}
```

## MQTT Quality of Service

| Topic group | QoS |
|---|---:|
| Fast telemetry | 0 |
| GPS | 0 |
| Lap timing | 1 |
| Status | 1 |
| Events | 1 |

JSON will be used during initial development because it is easy to read and debug.

## Shared live lap service

`lap_timer.py` runs the authoritative service; dashboards consume its state.

| Topic | QoS | Retained | Purpose |
| --- | --- | --- | --- |
| `umf1/car01/telemetry/lap` | 1 | No | Valid completed-lap event |
| `umf1/car01/telemetry/lap/state` | 1 | Yes | Full snapshot, every 2 s and at boundaries/on request |
| `umf1/car01/telemetry/lap/update` | 0 | No | 10 Hz append update and current metrics |
| `umf1/car01/telemetry/lap/request` | 1 | No | `{}` requests a fresh full snapshot |
| `umf1/car01/telemetry/lap/reference` | 1 | No | `{"session_id":"source boot ID","lap":2}` selects a reference; `lap:null` selects automatic best |

State envelopes contain `version:1`, a service-instance `stream` ID, increasing
`sequence`, `full`, and `state`. Append arrays use `{ "from": previous_length,
"items": [...] }`. A sequence/length mismatch requires a fresh snapshot. Retained
snapshots can populate historical data but never certify live GPS. Commands and
raw samples received as retained messages are ignored.

Gateway status uses `wall_time_ms` for Unix epoch milliseconds. Sensor
`timestamp_ms` remains a uint32 acquisition counter; these are different clocks.
Hardware fast/GPS messages need a shared boot/session ID and timestamp domain for
input alignment. GPS may include `throttle_percent`, `brake_bar`,
`steering_degrees` and `speed_kmh` captured at the fix time.

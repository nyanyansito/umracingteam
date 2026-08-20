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
  "timestamp_ms": 15200,
  "latitude": 3.1215,
  "longitude": 101.6532,
  "speed_kmh": 63.4,
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
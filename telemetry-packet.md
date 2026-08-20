# STM32-to-ESP32 Telemetry Packet

## Purpose

This document defines the initial binary packet sent from the STM32 to the ESP32 through UART.

Both the real hardware and the Python simulators must follow the same packet structure.

## General Packet Structure

| Field | Size | Description |
|---|---:|---|
| Start marker | 2 bytes | Marks the beginning of a packet |
| Protocol version | 1 byte | Identifies the packet-format version |
| Message type | 1 byte | Identifies the type of telemetry |
| Payload length | 2 bytes | Number of bytes in the payload |
| Sequence number | 2 bytes | Increases for every packet |
| Timestamp | 4 bytes | STM32 time in milliseconds |
| Payload | Variable | Contains the sensor values |
| CRC-16 | 2 bytes | Detects corrupted data |

## Initial Settings

| Setting | Value |
|---|---|
| Start marker | 0xAA55 |
| Protocol version | 1 |
| Byte order | Little-endian |
| Error detection | CRC-16 |
| Timestamp unit | Milliseconds |

## Message Types

| Message type | Number | Purpose |
|---|---:|---|
| Fast telemetry | 1 | Throttle, brake, steering and speed |
| GPS | 2 | Coordinates, GPS speed and time |
| Status | 3 | System health and error counters |
| Event | 4 | Important faults or vehicle events |

## Fast Telemetry Payload

Message type 1 contains the following values:

| Order | Channel | Stored type | Scaling |
|---:|---|---|---|
| 1 | Throttle position | uint16 | Value divided by 100 gives percent |
| 2 | Brake pressure | uint16 | Value divided by 100 gives bar |
| 3 | Steering angle | int16 | Value divided by 10 gives degrees |
| 4 | Vehicle speed | uint16 | Value divided by 100 gives km/h |

The fast telemetry payload is 8 bytes.

## Scaling Examples

A throttle value of 42.50% is stored as:

42.50 × 100 = 4250

A brake-pressure value of 75.25 bar is stored as:

75.25 × 100 = 7525

A steering-angle value of -18.5 degrees is stored as:

-18.5 × 10 = -185

A vehicle-speed value of 63.40 km/h is stored as:

63.40 × 100 = 6340

## Sequence Number

The sequence number increases by one for every packet.

Example:

100, 101, 102, 103

If the ESP32 receives:

100, 101, 103

then packet 102 may have been lost.

After reaching its maximum value, the sequence number returns to zero.

## Timestamp

The timestamp records when the STM32 created the packet.

It is measured in milliseconds since the STM32 started.

Example:

5000 milliseconds means the STM32 has been running for 5 seconds.

## CRC-16

CRC-16 is used to detect accidental changes or corruption during transmission.

The STM32 calculates and adds the CRC.

The ESP32 calculates it again after receiving the packet.

If the two CRC values do not match, the ESP32 rejects the packet.

The exact CRC-16 variant will be finalized before implementation.
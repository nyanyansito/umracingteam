# STM32-to-ESP32 UART Interface

## Purpose

The STM32 collects and processes vehicle sensor data.

The STM32 sends telemetry packets to the ESP32 through UART.

The ESP32 receives these packets and publishes them to the pit-wall laptop using Wi-Fi and MQTT.

## Proposed Physical Connections

| STM32 Connection | ESP32 Connection | Purpose |
|---|---|---|
| UART TX | UART RX | STM32 sends data to ESP32 |
| UART RX | UART TX | ESP32 sends commands or acknowledgements to STM32 |
| GND | GND | Shared electrical ground |

The final STM32 and ESP32 pin numbers will be selected when the hardware design is confirmed.

## Initial UART Settings

| Setting | Value |
|---|---|
| Baud rate | 921600 baud |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None initially |
| Packet type | Binary |
| Error detection | CRC-16 |

This configuration may also be described as 921600 8N1.

## Device Responsibilities

### STM32

- Read and process sensor values
- Add a timestamp
- Add a packet sequence number
- Pack values into telemetry packets
- Calculate the CRC-16
- Send the packets through UART

### ESP32

- Receive UART data
- Find complete packets
- Check the CRC-16
- Detect missing packet sequence numbers
- Connect to Wi-Fi
- Publish valid packets using MQTT
- Report communication errors

## Development Without Hardware

Before the physical devices are available:

1. A Python STM32 simulator will generate telemetry packets.
2. A virtual serial connection will represent the UART connection.
3. A Python ESP32 simulator will receive the packets.
4. The ESP32 simulator will publish the data to MQTT.

The simulators must use the same packet definition planned for the real STM32 and ESP32.

## Notes

UART is the initial interface because it is simple to develop, test, and debug.

SPI may be considered later only if UART cannot provide sufficient performance.
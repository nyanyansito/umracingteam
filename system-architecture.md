# UM F1 Pit-Wall Telemetry Architecture

## Data Flow

Sensors → STM32 → UART → ESP32 → Wi-Fi/MQTT → Mosquitto → Python → Graphical Software

## Component Responsibilities

### STM32

- Read vehicle sensors
- Apply sensor calibration
- Add timestamps
- Create telemetry packets
- Send packets to the ESP32 through UART

### ESP32

- Receive UART packets from the STM32
- Check that packets are valid
- Connect to the pit-wall Wi-Fi
- Publish telemetry to the MQTT broker

### Mosquitto

- Run on the pit-wall laptop
- Receive MQTT messages
- Deliver messages to subscribed programs

### Python Bridge

- Subscribe to MQTT topics
- Decode telemetry messages
- Detect missing or invalid data
- Save session logs
- Provide data to the graphical software

### Graphical Software

- Display live telemetry
- Show graphs
- Display lap and sector times
- Support post-session analysis
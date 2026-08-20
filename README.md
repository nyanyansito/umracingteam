# Pit Wall Telemetry System

This project simulates and develops the telemetry pipeline:

STM32 → ESP32 → MQTT → PC Subscriber → Data Processing → Dashboard

## Requirements
- Python 3
- Mosquitto
- paho-mqtt
- pandas
- MQTT Explorer

## How to Run
1. Start Mosquitto broker
2. Run stm32_simulator.py
3. Run esp32_gateway_sim.py
4. Run telemetry_subscriber.py
5. Run dashboard.py

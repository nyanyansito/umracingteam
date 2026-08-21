# Pit Wall Telemetry System

This project simulates and develops the telemetry pipeline:

Simulated STM32
→ binary UART packet
→ simulated ESP32
→ MQTT
→ logging
→ live dashboard
→ GPS and lap timing
→ export

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

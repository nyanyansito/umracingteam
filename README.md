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

## How to Run (run in terminal accordingly)
1. Start Mosquitto broker (should be already running in background if not, Start-Service -Name mosquitto)
2. type python stm32_link_server.py
3. type python esp32_simulator.py
4. type python gps_simulator.py
5. type python lap_timer.py
6. type python telemetry_logger.py
7. type python data_processing.py
8. type python dashboard.py ( open http://127.0.0.1:8050 to view)
9. type python mqtt_subscriber.py

TO STOP CTRL + C IN ALL TERMINALS
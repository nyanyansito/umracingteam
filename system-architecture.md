# Telemetry architecture

```text
STM32 simulator -> binary TCP/UART stand-in -> ESP32 gateway -> MQTT fast stream
GPS simulator --------------------------------------------> MQTT GPS stream
                                                               |
                 +---------------------+-----------------------+
                 |                     |                       |
          Data processor        Lap analysis service     Lossless logger
          range/shape flags     (python lap_timer.py)    CSV + topic JSONL
                 |              timing, checkpoints,
                 |              input alignment, delta,
                 |              persisted references
                 |                     |
                 +-------> Live Dash dashboard <----- snapshots + appends
                           100 Hz rolling input data
                           10 Hz sensor display updates
                           5 Hz lap/map display updates
                           local animated clock
```

Only the lap service owns timing and lap numbering. The dashboard is a subscriber;
late viewers receive a coherent snapshot and recover missed appends explicitly.
Completed laps are atomically saved; partial laps re-arm after service restart.

The launcher provides a shared clock origin/session ID to both simulators.
Hardware must provide the same acquisition clock/boot identity across fast and GPS
streams, or embed synchronised driver inputs with GPS. Physical serial/firmware
backends and sensor calibration remain hardware integration work. Use the JSON
configuration for the actual circuit and broker.

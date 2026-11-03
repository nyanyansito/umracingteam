"""Demo: show what data_processing.py does with a fake out-of-range value.

Run this on its own -- no Mosquitto, no simulators, no other terminals
needed. It calls the same functions the live pipeline uses
(build_processed_message) with a hand-made fake packet, so you can see
and screenshot exactly what "flag but don't modify" looks like.
"""

import json

from data_processing import build_processed_message, FAST_TOPIC


print("=" * 60)
print("DEMO: processing a FAKE telemetry packet with a bad value")
print("=" * 60)

# A fake "fast telemetry" packet -- as if it came straight off MQTT.
# throttle_percent is deliberately impossible (300%) to simulate a
# sensor glitch or corrupted packet. Everything else is normal.
fake_raw_packet = {
    "sequence": 9999,
    "timestamp_ms": 123456,
    "throttle_percent": 300.0,   # <-- impossible, channel-list.csv max is 100
    "brake_bar": 12.5,           # normal
    "steering_degrees": -10.0,   # normal
    "speed_kmh": 55.0,           # normal
}

print("\n1) RAW packet as received from MQTT:")
print(json.dumps(fake_raw_packet, indent=2))

processed_message = build_processed_message(FAST_TOPIC, fake_raw_packet)

print("\n2) PROCESSED message published by data_processing.py:")
print(json.dumps(processed_message, indent=2))

print("\n3) What this shows:")
print(
    f"   - throttle_percent is still 300.0 in the processed output "
    f"(UNCHANGED -- not clamped, not deleted)"
)
print(
    f"   - but range_flags marks it 'out_of_range', while every "
    f"normal field is marked 'ok'"
)
print(
    f"   - any_out_of_range = "
    f"{processed_message['any_out_of_range']} at the top level, so "
    f"downstream tools can catch it at a glance"
)
print("=" * 60)
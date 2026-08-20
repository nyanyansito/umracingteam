import math
import time


UPDATE_RATE_HZ = 10
UPDATE_INTERVAL_SECONDS = 1 / UPDATE_RATE_HZ


def generate_telemetry(sequence, elapsed_seconds):
    throttle = 50 + 50 * math.sin(elapsed_seconds)

    steering = 45 * math.sin(elapsed_seconds * 0.5)

    speed = 60 + 20 * math.sin(elapsed_seconds * 0.3)

    cycle_position = elapsed_seconds % 8
    if 5 <= cycle_position < 6:
        brake = 80
    else:
        brake = 0

    return {
        "sequence": sequence,
        "timestamp_ms": int(elapsed_seconds * 1000),
        "throttle_percent": round(throttle, 2),
        "brake_bar": round(brake, 2),
        "steering_degrees": round(steering, 2),
        "speed_kmh": round(speed, 2),
    }


print("Virtual STM32 started")
print("Press Control + C to stop")

start_time = time.monotonic()
sequence = 0

try:
    while True:
        elapsed = time.monotonic() - start_time
        telemetry = generate_telemetry(sequence, elapsed)

        print(telemetry)

        sequence += 1
        time.sleep(UPDATE_INTERVAL_SECONDS)

except KeyboardInterrupt:
    print()
    print("Virtual STM32 stopped")
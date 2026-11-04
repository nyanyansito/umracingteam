import time
from simulated_track import sample_track


UPDATE_RATE_HZ = 100
UPDATE_INTERVAL_SECONDS = 1 / UPDATE_RATE_HZ


def generate_telemetry(sequence, elapsed_seconds):
    sample = sample_track(elapsed_seconds)
    return {
        "sequence": sequence,
        "timestamp_ms": int(elapsed_seconds * 1000) % (1 << 32),
        **{field: round(sample[field], 2) for field in
           ("throttle_percent", "brake_bar", "steering_degrees", "speed_kmh")},
    }


def main():
    print("Virtual STM32 started")
    print("Press Control + C to stop")

    start_time = time.monotonic()
    sequence = 0

    try:
        while True:
            elapsed = time.monotonic() - start_time
            telemetry = generate_telemetry(sequence, elapsed)

            print(telemetry)

            sequence = (sequence + 1) % 65536
            time.sleep(UPDATE_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print()
        print("Virtual STM32 stopped")


if __name__ == "__main__":
    main()

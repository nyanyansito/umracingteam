class SequenceTracker:

    def __init__(self):
        self.last_sequence = None

    def check(self, sequence):
        if self.last_sequence is None:
            self.last_sequence = sequence

            return {
                "status": "first",
                "missed_packets": 0,
            }

        if sequence == self.last_sequence:
            return {
                "status": "duplicate",
                "missed_packets": 0,
            }

        expected_sequence = (
            self.last_sequence + 1
        ) % 65536

        if sequence == expected_sequence:
            self.last_sequence = sequence

            return {
                "status": "ok",
                "missed_packets": 0,
            }

        missed_packets = (
            sequence - expected_sequence
        ) % 65536

        self.last_sequence = sequence

        return {
            "status": "gap",
            "missed_packets": missed_packets,
            "expected_sequence": expected_sequence,
            "received_sequence": sequence,
        }


if __name__ == "__main__":
    tracker = SequenceTracker()

    example_sequences = [
        100,
        101,
        103,
        103,
        104,
    ]

    for example_sequence in example_sequences:
        result = tracker.check(example_sequence)

        print(
            f"Sequence {example_sequence}: {result}"
        )
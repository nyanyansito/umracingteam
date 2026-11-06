"""Source identity and ordering, independent of message arrival time."""
from collections import deque
from telemetry_validation import is_uint


def sample_key(sample):
    return (sample.get("session_id"), sample.get("sequence"), sample.get("timestamp_ms"))


class SampleOrder:
    def __init__(self):
        self.session = None
        self.timestamp = None
        self.sequence = None
        self.unwrapped_ms = 0
        self.retired = deque(maxlen=64)
        self.missed = 0
        self.rejected = 0
        self.generation = 0

    def check(self, sample):
        timestamp, sequence, session = (sample.get(k) for k in ("timestamp_ms", "sequence", "session_id"))
        if not is_uint(timestamp, 32) or (sequence is not None and not is_uint(sequence, 16)):
            raise ValueError("Invalid source counters")
        if session is not None and not isinstance(session, str):
            raise ValueError("Invalid source session")
        if session is not None and session in self.retired:
            self.rejected += 1
            return "late"
        changed = session is not None and self.timestamp is not None and session != self.session
        delta = None if self.timestamp is None else (timestamp - self.timestamp) % (1 << 32)
        # Legacy sources without a boot ID must restart their sequence at zero.
        legacy_reset = (session is None and sequence == 0 and delta is not None and delta > (1 << 31))
        if self.timestamp is None:
            status = "first"
        elif changed or legacy_reset:
            status = "reset"
        elif delta == 0 or (sequence is not None and sequence == self.sequence):
            self.rejected += 1
            return "duplicate"
        elif delta > (1 << 31) or (session is None and self.session is not None):
            self.rejected += 1
            return "late"
        else:
            status = "ok"
            if sequence is not None and self.sequence is not None:
                gap = (sequence - self.sequence) % 65536
                # Timestamp progress disambiguates a counter wrapping more than once.
                if 1 < gap < 32768:
                    self.missed += gap - 1
                    status = "gap"
        if status in ("first", "reset"):
            if changed and self.session is not None:
                self.retired.append(self.session)
            self.unwrapped_ms = timestamp
            self.generation += 1
            self.missed = 0
        else:
            self.unwrapped_ms += delta
        self.timestamp, self.sequence, self.session = timestamp, sequence, session
        return status

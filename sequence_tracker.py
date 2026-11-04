"""Per-stream uint16 sequencing with restart and late-packet handling."""
from telemetry_validation import is_uint


class SequenceTracker:
    def __init__(self):
        self.last_sequence = None
        self.last_timestamp_ms = None
        self.session_id = None
        self._retired_sessions = set()

    def check(self, sequence, timestamp_ms=None, session_id=None):
        if not is_uint(sequence, 16):
            raise ValueError("sequence must be uint16")
        if timestamp_ms is not None and not is_uint(timestamp_ms, 32):
            raise ValueError("timestamp_ms must be uint32")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string")

        if session_id is not None and session_id in self._retired_sessions:
            return {"status": "late", "missed_packets": 0}
        if self.last_sequence is None:
            status = "first"
        elif session_id is not None and session_id != self.session_id:
            if self.session_id is not None:
                self._retired_sessions.add(self.session_id)
            status = "reset"
        elif (session_id is None and sequence == 0 and timestamp_ms is not None
              and self.last_timestamp_ms is not None
              and timestamp_ms < self.last_timestamp_ms
              and self.last_timestamp_ms - timestamp_ms < (1 << 31)):
            # Legacy publishers: a zero counter plus regressing clock signals
            # a reboot. Other backward packets never count as huge losses.
            status = "reset"
        else:
            distance = (sequence - self.last_sequence) % 65536
            if distance == 0:
                return {"status": "duplicate", "missed_packets": 0}
            if distance >= 32768:
                return {"status": "late", "missed_packets": 0}
            status = "ok" if distance == 1 else "gap"

        result = {"status": status, "missed_packets": 0}
        if status == "gap":
            result.update(missed_packets=distance - 1,
                          expected_sequence=(self.last_sequence + 1) % 65536,
                          received_sequence=sequence)
        self.last_sequence = sequence
        self.last_timestamp_ms = timestamp_ms
        self.session_id = session_id
        return result

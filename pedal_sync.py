"""Match GNSS epochs and pedal acquisition times in STM32 milliseconds."""
from collections import deque

MOD = 2**32
MAX_MATCH_MS = 15


def elapsed(a, b):
    return (a - b + MOD // 2) % MOD - MOD // 2


class PedalClock:
    def __init__(self):
        self.samples = {key: deque(maxlen=128) for key in ("T", "B")}
        self.first = self.last = None
        self.uptime = 0
        self.fix_seconds = None

    def add(self, key, tick, value):
        if self.first is None:
            self.first = self.last = tick
        step = elapsed(tick, self.last)
        if step < 0:
            return None
        self.last = tick
        self.uptime += step
        self.samples[key].append((tick, value))
        return self.uptime / 1000

    def rate(self, key):
        items = self.samples[key]
        if len(items) < 2:
            return None
        duration = elapsed(items[-1][0], items[0][0])
        return (len(items) - 1) * 1000 / duration if duration > 0 else None

    def match(self, rx, pulse, fraction):
        self.fix_seconds = None
        values = {"throttle_percent": None, "brake_percent": None}
        if pulse is None or not 0 <= elapsed(rx, pulse) < 1050:
            return values, "Waiting for PPS", None
        # RMC may cross the next PPS boundary in transit. Reject implausible
        # serial lag rather than using a pedal sampled when the sentence arrived.
        fix_tick = (pulse + fraction) % MOD
        if elapsed(rx, fix_tick) < 0:
            fix_tick = (fix_tick - 1000) % MOD
        if not 0 <= elapsed(rx, fix_tick) <= 350:
            return values, "GPS sentence timing uncertain", None
        if self.last is not None:
            self.fix_seconds = (self.uptime + elapsed(fix_tick, self.last)) / 1000
        offsets = []
        for key, field in (("T", "throttle_percent"), ("B", "brake_percent")):
            nearest = min(self.samples[key], key=lambda p: abs(elapsed(p[0], fix_tick)), default=None)
            if nearest is not None:
                offset = abs(elapsed(nearest[0], fix_tick))
                if offset <= MAX_MATCH_MS and nearest[1] is not None:
                    values[field] = nearest[1]
                    offsets.append(offset)
        return values, "PPS aligned" if len(offsets) == 2 else "Pedal match incomplete", max(offsets, default=None)

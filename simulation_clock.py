"""A bounded-rate virtual clock: host stalls pause the simulated car."""
import time
import os


class SimulationClock:
    def __init__(self, interval):
        self.interval = interval
        self.steps = 0
        self.deadline = time.monotonic()
        epoch = os.environ.get("UMR_SIM_START_SECONDS")
        self.shared_start = float(epoch) if epoch is not None else None
        if self.shared_start is not None and self.shared_start > self.deadline:
            # Do not publish timestamp-zero packets before the shared clock starts:
            # they would make the initial source/receive offset look artificially old.
            time.sleep(self.shared_start - self.deadline)
            self.deadline = self.shared_start

    @property
    def elapsed(self):
        if self.shared_start is not None:
            return max(0, time.monotonic() - self.shared_start)
        return self.steps * self.interval

    def advance(self):
        self.steps += 1
        now = time.monotonic()
        self.deadline += self.interval
        # Do not teleport along the track or flood MQTT with a catch-up burst.
        if self.deadline < now:
            self.deadline = now + self.interval
        time.sleep(max(0, self.deadline - now))

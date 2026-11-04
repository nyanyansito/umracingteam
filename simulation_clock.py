"""A bounded-rate virtual clock: host stalls pause the simulated car."""
import time


class SimulationClock:
    def __init__(self, interval):
        self.interval = interval
        self.steps = 0
        self.deadline = time.monotonic()

    @property
    def elapsed(self):
        return self.steps * self.interval

    def advance(self):
        self.steps += 1
        now = time.monotonic()
        self.deadline += self.interval
        # Do not teleport along the track or flood MQTT with a catch-up burst.
        if self.deadline < now:
            self.deadline = now + self.interval
        time.sleep(max(0, self.deadline - now))

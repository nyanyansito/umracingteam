"""Keeps out-of-range warnings visible long enough for a person to see.

Why this exists
---------------
The fast telemetry stream sends about 10 messages a second. If one bad
value arrives, the very next normal message arrives ~100 ms later and
would instantly "clear" the warning -- the dashboard would flash it for
a blink and a human would never see it.

AlertTracker fixes that: when a processed message says a field is out of
range, the alert is remembered and stays "active" for HOLD_SECONDS. If
the same field goes bad again, the timer simply restarts.

This file has no MQTT and no web code in it on purpose, so it can be
unit tested on its own (see test_flag_alerts.py) and reused by the
dashboard.
"""

import time


HOLD_SECONDS = 5.0


class AlertTracker:

    def __init__(self, hold_seconds=HOLD_SECONDS):
        self.hold_seconds = hold_seconds
        self._alerts = {}
        self.processed_seen = False

    def update(self, processed_message, now=None):
        """Feed in one processed message (as published by
        data_processing.py). Any field flagged "out_of_range" becomes
        (or stays) an active alert.
        """
        if now is None:
            now = time.monotonic()

        self.processed_seen = True

        range_flags = processed_message.get("range_flags", {})
        values = processed_message.get("values", {})
        limits = processed_message.get("limits", {})

        for field_name, flag in range_flags.items():
            if flag == "ok":
                continue

            field_limits = limits.get(field_name, {})

            self._alerts[field_name] = {
                "value": values.get(field_name),
                "min": field_limits.get("min"),
                "max": field_limits.get("max"),
                "time": now,
            }

    def active(self, now=None):
        """Return {field_name: details} for alerts still within the
        hold time. Expired alerts are dropped. Each details dict has
        value, min, max and age_seconds.
        """
        if now is None:
            now = time.monotonic()

        self._alerts = {
            field_name: alert
            for field_name, alert in self._alerts.items()
            if now - alert["time"] <= self.hold_seconds
        }

        return {
            field_name: {
                "value": alert["value"],
                "min": alert["min"],
                "max": alert["max"],
                "age_seconds": now - alert["time"],
            }
            for field_name, alert in self._alerts.items()
        }


def describe_alert(alert):
    """One short human sentence for an active alert, e.g.
    'Got 300.00 (allowed 0 to 100) - 2s ago'.
    """
    value = alert["value"]
    value_text = f"{value:.2f}" if isinstance(value, (int, float)) else str(value)

    low, high = alert["min"], alert["max"]
    if low is not None and high is not None:
        allowed = f" (allowed {low:g} to {high:g})"
    elif high is not None:
        allowed = f" (allowed up to {high:g})"
    elif low is not None:
        allowed = f" (allowed from {low:g})"
    else:
        allowed = ""

    return f"Got {value_text}{allowed} - {alert['age_seconds']:.0f}s ago"
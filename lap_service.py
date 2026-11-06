"""Authoritative live lap analysis, input alignment and completed-lap storage."""
from collections import deque
import json
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from lap_analysis import LapAnalysis
from lap_timer import GPS_TOPIC, LAP_TOPIC
from lap_stream import STATE_TOPIC, UPDATE_TOPIC, REQUEST_TOPIC, REFERENCE_TOPIC, make_update
from mqtt_runtime import start_mqtt
from sample_order import SampleOrder
from session_store import restore_session, save_session
from telemetry_config import BROKER, PORT, STATE_FILE
from telemetry_validation import FAST_FIELDS, decode_object, is_number, validate_fast_sample

FAST_TOPIC = "umf1/car01/telemetry/fast"


class InputBuffer:
    """Only join acquisition timestamps from the same source boot/session."""
    def __init__(self):
        self.samples = deque(maxlen=2000)
        self.order = SampleOrder()

    def add(self, sample):
        validate_fast_sample(sample)
        status = self.order.check(sample)
        if status in ("late", "duplicate"):
            return
        if status == "reset":
            self.samples.clear()
        self.samples.append(sample.copy())

    def align(self, gps):
        result = gps.copy()
        session, timestamp = gps.get("session_id"), gps.get("timestamp_ms")
        if all(is_number(gps.get(k)) for k in FAST_FIELDS):
            return result
        if not isinstance(session, str) or type(timestamp) is not int:
            return result
        nearest = None
        age = 151
        for sample in reversed(self.samples):
            if sample.get("session_id") != session:
                continue
            difference = (timestamp - sample["timestamp_ms"]) % (1 << 32)
            distance = min(difference, (1 << 32) - difference)
            if distance < age:
                age, nearest = distance, sample
        if nearest is not None:
            for field in FAST_FIELDS:
                if not is_number(result.get(field)):
                    result[field] = nearest[field]
        return result


class LapService:
    def __init__(self, client, path=STATE_FILE, analysis=None):
        self.client = client
        self.analysis = analysis or LapAnalysis()
        self.path = path
        self.lock = threading.RLock()
        self.inputs = InputBuffer()
        self.stream = uuid.uuid4().hex
        self.sequence = 0
        self.previous = None
        self.request_snapshot = True
        self.last_snapshot = -float("inf")
        self.saved_revision = self.analysis.revision
        self.connected = threading.Event()
        if path is not None:
            try:
                restore_session(path, self.analysis)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"Cannot restore lap session: {error}")
        self.saved_revision = self.analysis.revision

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            for topic in (GPS_TOPIC, FAST_TOPIC, REQUEST_TOPIC, REFERENCE_TOPIC):
                client.subscribe(topic, qos=1 if topic in (REQUEST_TOPIC, REFERENCE_TOPIC) else 0)
            with self.lock:
                self.request_snapshot = True
            self.connected.set()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        with self.lock:
            self.analysis.interrupt()
            self.request_snapshot = True

    def on_message(self, client, userdata, message):
        if getattr(message, "retain", False):
            return
        try:
            data = decode_object(message.payload)
            with self.lock:
                if message.topic == FAST_TOPIC:
                    self.inputs.add(data)
                elif message.topic == GPS_TOPIC:
                    before = self.analysis.completed_count
                    self.analysis.update(self.inputs.align(data))
                    if self.analysis.completed_count > before:
                        client.publish(LAP_TOPIC, json.dumps(self.analysis.history[-1], allow_nan=False), qos=1)
                elif message.topic == REQUEST_TOPIC:
                    self.request_snapshot = True
                elif message.topic == REFERENCE_TOPIC and data.get("session_id") == self.analysis.session_id:
                    self.analysis.select_reference(data.get("lap"))
                    self.request_snapshot = True
        except (ValueError, UnicodeDecodeError, TypeError) as error:
            if message.topic == GPS_TOPIC:
                with self.lock:
                    self.analysis.interrupt()
            print(f"Ignored invalid lap input on {message.topic}: {error}")

    def publish_state(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            state = self.analysis.snapshot(now)
            full = (self.previous is None or self.request_snapshot or now - self.last_snapshot >= 2
                    or state["lap_key"] != self.previous["lap_key"]
                    or state["revision"] != self.previous["revision"])
            packet = {"version": 1, "stream": self.stream, "sequence": self.sequence,
                      "full": full, "state": state if full else make_update(self.previous, state)}
            result = self.client.publish(STATE_TOPIC if full else UPDATE_TOPIC,
                                         json.dumps(packet, allow_nan=False), qos=1 if full else 0, retain=full)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.sequence += 1
                self.previous = state
                if full:
                    self.last_snapshot = now
                    self.request_snapshot = False
            if self.path is not None and self.saved_revision != self.analysis.revision:
                try:
                    save_session(self.path, self.analysis)
                    self.saved_revision = self.analysis.revision
                except OSError as error:
                    print(f"Could not save completed laps: {error}")
            return packet


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="umf1-lap-service")
    service = LapService(client)
    client.on_connect = service.on_connect
    client.on_disconnect = service.on_disconnect
    client.on_message = service.on_message
    start_mqtt(client, BROKER, PORT)
    print(f"Live lap service started; completed laps saved to {STATE_FILE}")
    try:
        while True:
            start = time.monotonic()
            if service.connected.is_set():
                service.publish_state(start)
            time.sleep(max(.01, .1 - (time.monotonic() - start)))
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()

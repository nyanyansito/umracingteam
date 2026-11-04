"""Automatic check of the dashboard warning (run it with the pipeline running).

Start these first, each in its own terminal (see how_to_run_and_test_warning.md):
  Mosquitto, stm32_link_server.py, esp32_simulator.py,
  data_processing.py, dashboard.py

Then run:   python check_dashboard_warning.py

It does NOT need a browser. It asks the dashboard what it would show
  1) before any fake data            -> no warnings
  2) right after a fake bad packet   -> warning triangles on the bad boxes
  3) ~6 seconds later                -> warnings cleared again
and prints PASS/FAIL for every check. Takes about 10 seconds.
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import json, time, urllib.request
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish

BASE = "http://127.0.0.1:8050"

def get(path):
    return json.load(urllib.request.urlopen(BASE + path))

deps = [d for d in get("/_dash-dependencies") if "throttle-warning" in d["output"]][0]
out_ids = [o for o in deps["output"].strip(".").split("...")]
outputs = [{"id": o.rsplit(".", 1)[0], "property": o.rsplit(".", 1)[1]} for o in out_ids]

def dashboard_state(tick=1):
    payload = {
        "output": deps["output"],
        "outputs": outputs,
        "inputs": [{"id": "dashboard-update", "property": "n_intervals", "value": tick}],
        "changedPropIds": ["dashboard-update.n_intervals"],
    }
    req = urllib.request.Request(
        BASE + "/_dash-update-component",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = json.load(urllib.request.urlopen(req))["response"]
    return resp

def summarize(r):
    lines = []
    for k in ("throttle", "brake", "steering", "speed"):
        title = r[f"{k}-title"]["children"]
        warn = r[f"{k}-warning"]["children"]
        shadow = r[f"{k}-box"]["style"].get("boxShadow", "-")
        lines.append(f"  {title:<22} value={r[k+'-value']['children']:<12} box-outline={shadow:<22} warning='{warn}'")
    lines.append(f"  SUMMARY BAR: {r['validation-status']['children']}")
    return "\n".join(lines)

results = []
def check(name, cond):
    results.append((name, cond))
    print(("  PASS  " if cond else "  FAIL  ") + name)

# capture flagged processed messages from the broker
captured = []
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="e2e-capture")
c.on_message = lambda cl, u, m: captured.append(json.loads(m.payload))
c.connect("localhost", 1883); c.subscribe("umf1/car01/telemetry/fast/processed"); c.loop_start()

print("\n=== PHASE 1: before any fake data (normal running) ===")
time.sleep(1.5)
r = dashboard_state()
print(summarize(r))
check("no warning triangles on any box", all("\u26a0" not in r[f"{k}-title"]["children"] for k in ("throttle","brake","steering","speed")))
check("summary bar is green 'all channels in range'", "all channels in range" in r["validation-status"]["children"])
check("normal live values still shown (original behaviour kept)", r["throttle-value"]["children"].endswith("%") and r["speed-value"]["children"].endswith("km/h"))

print("\n=== PHASE 2: inject FAKE packet (throttle 300 %, steering -90 deg) ===")
publish.single("umf1/car01/telemetry/fast", json.dumps(
    {"sequence": 1, "timestamp_ms": 0, "throttle_percent": 300.0, "brake_bar": 0,
     "steering_degrees": -90.0, "speed_kmh": 50.0}), hostname="localhost")
time.sleep(1.0)   # normal packets keep arriving after the fake one
r = dashboard_state(2)
print(summarize(r))
check("throttle box shows warning triangle", r["throttle-title"]["children"].startswith("\u26a0"))
check("steering box shows warning triangle", r["steering-title"]["children"].startswith("\u26a0"))
check("brake box has NO triangle (it was fine)", "\u26a0" not in r["brake-title"]["children"])
check("speed box has NO triangle (it was fine)", "\u26a0" not in r["speed-title"]["children"])
check("warning text shows the bad value and the allowed range", "300.00" in r["throttle-warning"]["children"] and "allowed 0 to 100" in r["throttle-warning"]["children"])
check("steering warning shows its own limits (-45 to 45)", "allowed -45 to 45" in r["steering-warning"]["children"])
check("flagged box gets red outline", r["throttle-box"]["style"].get("boxShadow") and "dc3545" in r["throttle-box"]["style"]["boxShadow"])
check("summary bar turns into a WARNING naming the channels", "WARNING" in r["validation-status"]["children"] and "Throttle" in r["validation-status"]["children"])
bad = [m for m in captured if m.get("any_out_of_range")]
check("broker carried a flagged processed message", len(bad) >= 1)
check("flagged value was passed through UNCHANGED (still 300.0)", bad and bad[0]["values"]["throttle_percent"] == 300.0)
check("processed message includes limits", bad and bad[0]["limits"]["throttle_percent"] == {"min": 0, "max": 100})

print("\n=== PHASE 3: wait for the warning to time out (hold = 5 s) ===")
time.sleep(5.5)
r = dashboard_state(3)
print(summarize(r))
check("triangles are gone after the hold time", all("\u26a0" not in r[f"{k}-title"]["children"] for k in ("throttle","brake","steering","speed")))
check("summary bar back to green", "all channels in range" in r["validation-status"]["children"])

c.loop_stop()
failed = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(failed)}/{len(results)} checks passed")
raise SystemExit(1 if failed else 0)
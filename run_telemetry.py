"""Start the complete local simulation: python run_telemetry.py."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from telemetry_config import BROKER, PORT, DASHBOARD_HOST, DASHBOARD_PORT, STM32_PORT

ROOT = Path(__file__).resolve().parent
PROGRAMS = (
    "data_processing.py", "telemetry_logger.py", "lap_timer.py", "dashboard.py",
    "stm32_link_server.py", "esp32_simulator.py", "gps_simulator.py",
)
BOOTSTRAP = (
    "import runpy,signal,sys; "
    "signal.signal(getattr(signal,'SIGBREAK',signal.SIGINT),signal.default_int_handler); "
    "runpy.run_path(sys.argv[1],run_name='__main__')"
)


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=.3):
            return True
    except (ConnectionRefusedError, TimeoutError):
        return False


def find_mosquitto(explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Mosquitto executable not found: {path}")
        return str(path)
    executable = shutil.which("mosquitto")
    if executable:
        return executable
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        folder = os.environ.get(variable)
        if folder:
            candidate = Path(folder) / "mosquitto" / "mosquitto.exe"
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError(
        "Mosquitto is not running and its executable was not found. Install Mosquitto, "
        'or pass --mosquitto-path "C:\\Program Files\\mosquitto\\mosquitto.exe".'
    )


class Launcher:
    def __init__(self, logs):
        self.logs = Path(logs)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.children = []
        self.environment = dict(os.environ, UMR_SIM_START_SECONDS=str(time.monotonic() + 3),
                                UMR_SIM_SESSION_ID=uuid.uuid4().hex)

    def spawn(self, name, command):
        log = (self.logs / f"{name}.log").open("w", encoding="utf-8")
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       env=self.environment, **options)
        except BaseException:
            log.close()
            raise
        self.children.append((name, process, log))
        print(f"Started {name} (PID {process.pid})", flush=True)
        return process

    def start_python(self, script):
        return self.spawn(Path(script).stem, [sys.executable, "-u", "-c", BOOTSTRAP, str(ROOT / script)])

    def check_children(self):
        for name, process, _ in self.children:
            code = process.poll()
            if code is not None:
                raise RuntimeError(f"{name} exited (code {code}). See {self.logs / (name + '.log')}")

    def wait_for_broker(self, timeout=10):
        deadline = time.monotonic() + timeout
        while not port_open(PORT):
            self.check_children()
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Mosquitto did not open port {PORT}. See {self.logs / 'mosquitto.log'}")
            time.sleep(.1)

    def stop(self):
        # Signal every owned process first, allowing parallel graceful cleanup.
        for _, process, _ in reversed(self.children):
            if process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                except OSError:
                    pass
        deadline = time.monotonic() + 5
        for _, process, log in reversed(self.children):
            try:
                if process.poll() is None:
                    try:
                        process.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            except ProcessLookupError:
                pass
            finally:
                log.close()
        self.children.clear()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mosquitto-path", help="Path to mosquitto.exe when not on PATH")
    parser.add_argument("--subscriber", action="store_true", help="Also run packet diagnostics in its own log")
    args = parser.parse_args(argv)
    launcher = None
    try:
        if sys.version_info < (3, 13):
            raise RuntimeError("Use Python 3.13 or newer, as required by this project.")
        missing = [name for name in ("paho", "dash", "plotly") if importlib.util.find_spec(name) is None]
        if missing:
            raise RuntimeError("Missing dependencies. Run: python -m pip install -r requirements.txt")
        for port in (STM32_PORT, DASHBOARD_PORT):
            if port_open(port):
                raise RuntimeError(f"Port {port} is already in use. Stop the existing simulator/dashboard before launching.")
        remote_broker = BROKER not in ("localhost", "127.0.0.1", "::1")
        broker_running = remote_broker or port_open(PORT)
        executable = None if broker_running else find_mosquitto(args.mosquitto_path)
        logs = ROOT / "logs" / ("launcher_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        launcher = Launcher(logs)
        print(f"Program logs: {logs}", flush=True)
        if broker_running:
            print(f"Using broker at {BROKER}:{PORT}; it will remain running after shutdown.")
        else:
            launcher.spawn("mosquitto", [executable, "-p", str(PORT), "-v"])
            launcher.wait_for_broker()
        for script in PROGRAMS + (("mqtt_subscriber.py",) if args.subscriber else ()):
            launcher.start_python(script)
        print(f"Dashboard starting at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}\nPress Ctrl+C here to stop the session.", flush=True)
        while True:
            launcher.check_children()
            time.sleep(.25)
    except KeyboardInterrupt:
        print("\nStopping telemetry session…", flush=True)
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Launcher error: {error}", file=sys.stderr)
        return 1
    finally:
        if launcher is not None:
            launcher.stop()


if __name__ == "__main__":
    raise SystemExit(main())

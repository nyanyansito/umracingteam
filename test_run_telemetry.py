import contextlib
import io
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import run_telemetry as launcher


class LauncherTests(unittest.TestCase):
    def run_quietly(self, args=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return launcher.main(args or [])

    def test_existing_broker_is_reused_and_only_python_programs_are_owned(self):
        instance = MagicMock()
        instance.check_children.side_effect = KeyboardInterrupt
        with patch.object(launcher, "Launcher", return_value=instance), patch.object(launcher, "port_open", side_effect=lambda port: port == 1883), patch.object(launcher.importlib.util, "find_spec", return_value=object()), patch.object(launcher, "find_mosquitto") as find:
            self.assertEqual(self.run_quietly(["--subscriber"]), 0)
        find.assert_not_called()
        instance.spawn.assert_not_called()
        self.assertEqual([call.args[0] for call in instance.start_python.call_args_list], list(launcher.PROGRAMS) + ["mqtt_subscriber.py"])
        instance.stop.assert_called_once()

    def test_new_broker_is_started_and_waited_for_before_clients(self):
        instance = MagicMock()
        instance.check_children.side_effect = KeyboardInterrupt
        with patch.object(launcher, "Launcher", return_value=instance), patch.object(launcher, "port_open", return_value=False), patch.object(launcher.importlib.util, "find_spec", return_value=object()), patch.object(launcher, "find_mosquitto", return_value="C:/mosquitto.exe"):
            self.assertEqual(self.run_quietly(), 0)
        instance.spawn.assert_called_once_with("mosquitto", ["C:/mosquitto.exe", "-p", "1883", "-v"])
        names = [call[0] for call in instance.mock_calls]
        self.assertLess(names.index("wait_for_broker"), names.index("start_python"))
        instance.stop.assert_called_once()

    def test_partial_startup_failure_cleans_up_started_processes(self):
        instance = MagicMock()
        instance.start_python.side_effect = [MagicMock(), OSError("could not start")]
        with patch.object(launcher, "Launcher", return_value=instance), patch.object(launcher, "port_open", side_effect=lambda port: port == 1883), patch.object(launcher.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(self.run_quietly(), 1)
        instance.stop.assert_called_once()

    def test_occupied_dashboard_port_prevents_starting_duplicates(self):
        with patch.object(launcher, "Launcher") as start, patch.object(launcher, "port_open", side_effect=lambda port: port == 8050), patch.object(launcher.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(self.run_quietly(), 1)
        start.assert_not_called()

    def test_child_failure_identifies_log_and_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            session = launcher.Launcher(folder)
            process = MagicMock()
            process.poll.return_value = 7
            session.children.append(("dashboard", process, MagicMock()))
            with self.assertRaisesRegex(RuntimeError, r"dashboard exited \(code 7\).*dashboard.log"):
                session.check_children()

    def test_default_windows_install_is_found_without_path(self):
        with patch.object(launcher.shutil, "which", return_value=None), patch.dict(launcher.os.environ, {"ProgramFiles": "C:/Program Files"}), patch.object(Path, "is_file", return_value=True):
            self.assertEqual(launcher.find_mosquitto(), str(Path("C:/Program Files/mosquitto/mosquitto.exe")))

    def test_graceful_stop_falls_back_to_termination_for_hung_child(self):
        with tempfile.TemporaryDirectory() as folder:
            session = launcher.Launcher(folder)
            process, log = MagicMock(), MagicMock()
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("test", 0), None]
            session.children.append(("test", process, log))
            session.stop()
            process.send_signal.assert_called_once()
            process.terminate.assert_called_once()
            log.close.assert_called_once()
            self.assertEqual(session.children, [])

    def test_real_child_process_is_reaped_and_log_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            session = launcher.Launcher(folder)
            with contextlib.redirect_stdout(io.StringIO()):
                process = session.spawn("sleep", [sys.executable, "-c", "import time; time.sleep(30)"])
            log = session.children[0][2]
            try:
                session.stop()
                self.assertIsNotNone(process.poll())
                self.assertTrue(log.closed)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()

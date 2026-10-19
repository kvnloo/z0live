import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "bin/phone-adb"
loader = importlib.machinery.SourceFileLoader("phone_adb", str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


class PhoneAdbTests(unittest.TestCase):
    def test_port_bounds_and_input(self):
        for value in ["0", "65536", "22;id", "abc", "", "-2"]:
            with self.subTest(value=value), self.assertRaises(Exception):
                m.port(value)
        self.assertEqual(m.port("46431"), 46431)

    def test_discovery_pins_hardware_serial_and_connect_service(self):
        text = "\n".join([
            "=;eth0;IPv4;adb-R5CY93G2WNH-abc;_adb-tls-connect._tcp;local;phone.local;192.168.1.30;46431;",
            "=;eth0;IPv6;adb-R5CY93G2WNH-abc;_adb-tls-connect._tcp;local;phone.local;::1;46431;",
            "=;eth0;IPv4;adb-OTHER-abc;_adb-tls-connect._tcp;local;other.local;192.168.1.31;40000;",
            "=;eth0;IPv4;adb-R5CY93G2WNH-abc;_adb-tls-pairing._tcp;local;phone.local;192.168.1.30;40001;",
        ])
        self.assertEqual(m.parse_discovery(text), [46431])

    def test_state_is_private_and_identity_checked(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, "STATE", Path(directory) / "state.json"):
            m.save(46431)
            self.assertEqual(m.load()["port"], 46431)
            self.assertEqual(m.STATE.stat().st_mode & 0o777, 0o600)
            m.STATE.write_text('{"host":"100.1.1.1","serial":"OTHER","port":46431}')
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                m.load()

    def test_connect_checks_state_not_adb_success_message(self):
        reply = subprocess.CompletedProcess([], 0, "connected", "")
        with patch.object(m, "adb", return_value=reply), patch.object(m, "verify", return_value=False), patch.object(m, "save") as save:
            with self.assertRaisesRegex(RuntimeError, "not authorized"):
                m.connect(46431)
            save.assert_not_called()

    def test_verify_rejects_different_hardware(self):
        ready = subprocess.CompletedProcess([], 0, "device\n", "")
        other = subprocess.CompletedProcess([], 0, "OTHER\n", "")
        with patch.object(m, "adb", side_effect=[ready, other]):
            with self.assertRaisesRegex(RuntimeError, "hardware serial"):
                m.verify(46431)

    def test_reconnect_falls_back_to_unique_lan_port(self):
        with patch.object(m, "load", return_value={"port": 40000}), patch.object(m, "discover", return_value=[46431]), patch.object(m, "connect", side_effect=[RuntimeError("offline"), 46431]) as connect:
            self.assertEqual(m.reconnect(), 46431)
            self.assertEqual(connect.call_args_list[-1].args, (46431,))

    def test_remote_without_mdns_fails_not_port_scans(self):
        with patch.object(m, "load", return_value={}), patch.object(m, "discover", return_value=[]), patch.object(m, "connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "mDNS does not cross Tailscale"):
                m.reconnect()
            connect.assert_not_called()

    def test_identity_mismatch_never_falls_back(self):
        with patch.object(m, "load", return_value={"port": 40000}), patch.object(m, "discover") as discover, patch.object(m, "connect", side_effect=m.PhoneIdentityError("wrong phone")):
            with self.assertRaises(m.PhoneIdentityError):
                m.reconnect()
            discover.assert_not_called()

    def test_cli_exec_pins_device_and_propagates_exit_code(self):
        with patch.object(sys, "argv", ["phone-adb", "exec", "shell", "true"]), patch.object(m, "load", return_value={"port": 46431}), patch.object(m, "verify", return_value=True), patch.object(m, "adb", return_value=subprocess.CompletedProcess([], 7)) as adb:
            self.assertEqual(m.main(), 7)
            adb.assert_called_once_with("-s", "100.117.226.39:46431", "shell", "true", capture=False, timeout=300)

    def test_cli_exec_refuses_server_commands_and_target_override(self):
        for command in [["kill-server"], ["devices"], ["connect", "100.1.1.1:5555"], ["-s", "OTHER", "shell", "true"], []]:
            with self.subTest(command=command), patch.object(sys, "argv", ["phone-adb", "exec", *command]), patch.object(m, "load", return_value={"port": 46431}), patch.object(m, "verify", return_value=True), patch.object(m, "adb") as adb:
                with self.assertRaises((RuntimeError, SystemExit)):
                    m.main()
                adb.assert_not_called()

    def test_cli_offline_exec_does_not_dispatch(self):
        with patch.object(sys, "argv", ["phone-adb", "exec", "shell", "true"]), patch.object(m, "load", return_value={"port": 46431}), patch.object(m, "verify", return_value=False), patch.object(m, "adb") as adb:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                m.main()
            adb.assert_not_called()

    def test_cli_pair_is_interactive_and_never_saves_code(self):
        with patch.object(sys, "argv", ["phone-adb", "pair", "40001"]), patch.object(m, "adb", return_value=subprocess.CompletedProcess([], 0)) as adb, patch.object(m, "save") as save:
            self.assertEqual(m.main(), 0)
            adb.assert_called_once_with("pair", "100.117.226.39:40001", timeout=120, capture=False)
            save.assert_not_called()

    def test_cli_env_selects_only_verified_android_with_telemetry_off(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["phone-adb", "env"]), patch.object(m, "load", return_value={"port": 46431}), patch.object(m, "verify", return_value=True), patch.object(m, "ADB", "/sdk with space/adb"), contextlib.redirect_stdout(output):
            self.assertEqual(m.main(), 0)
        exports = dict(shlex.split(line)[1].split("=", 1) for line in output.getvalue().splitlines())
        self.assertEqual(exports, {"ANDROID_SERIAL": "100.117.226.39:46431", "PHONE_HARNESS_PLATFORM": "android", "PHONE_HARNESS_TELEMETRY": "0", "PHONE_HARNESS_CLASSIFIER_AUTO_RUN": "0", "PHONE_HARNESS_ADB": "/sdk with space/adb"})

    def test_corrupt_state_cli_fails_cleanly_before_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "z0live/phone-adb.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"host": m.PHONE_IP, "serial": m.PHONE_SERIAL, "port": "oops"}))
            result = subprocess.run([sys.executable, str(PATH), "env"], env={**os.environ, "XDG_STATE_HOME": directory, "PHONE_ADB_BIN": "/does/not/exist"}, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Port must be an integer", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()

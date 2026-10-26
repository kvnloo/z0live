"""Offline negative controls for the REA S25 protocol; no REA or phone needed."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("s25_rea_protocol", HERE / "rea_protocol.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class S25ReaProtocolTests(unittest.TestCase):
    def test_dry_run_does_not_query_binary_or_phone(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m.subprocess, "run") as run:
            result = m.run_protocol(execute=False, phone=True, model_file=None,
                rea_bin="unavailable", phone_adb="unavailable",
                output_directory=Path(d) / "not-created")
            self.assertEqual(result["status"], "NOT_RUN")
            self.assertEqual(result["planned_operations"],
                ["doctor", "inspect-adb-client", "inspect-adb-device", "list-adb-features"])
            self.assertFalse(result["phone_accessed"])
            run.assert_not_called()

    def test_missing_rea_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m.shutil, "which", return_value=None), patch.object(m.subprocess, "run") as run:
            result = m.run_protocol(execute=True, phone=True, model_file=None,
                rea_bin="rea", phone_adb="phone-adb", output_directory=Path(d) / "evidence")
            self.assertEqual(result["status"], "CAPABILITY_UNAVAILABLE")
            self.assertFalse(result["phone_accessed"])
            run.assert_not_called()

    def test_verified_phone_env_accepts_quoted_path_not_commands(self):
        value = "export ANDROID_SERIAL='verified:port'\nexport PHONE_HARNESS_ADB='/sdk with space/adb'\nexport PHONE_HARNESS_TELEMETRY='0'\n"
        result = m.parse_verified_phone_env(value)
        self.assertEqual(result["PHONE_HARNESS_ADB"], "/sdk with space/adb")
        self.assertEqual(result["ANDROID_SERIAL"], "verified:port")

    def test_verified_phone_env_rejects_unknown_commands_and_incomplete_identity(self):
        cases = ["export ANDROID_SERIAL='abc'\n", "echo hello\n",
                 "export ANDROID_SERIAL='abc'; rm -rf /tmp/test\nexport PHONE_HARNESS_ADB=/sdk/adb\n",
                 "export ANDROID_SERIAL='abc'\nexport PHONE_HARNESS_ADB=/sdk/adb\nexport ANDROID_SERIAL='different'\n",
                 "export ANDROID_SERIAL='a\\nb'\nexport PHONE_HARNESS_ADB=/sdk/adb\n"]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(m.ProbeFailure):
                m.parse_verified_phone_env(value)

    def test_phone_preflight_fails_closed(self):
        with patch.object(m.shutil, "which", return_value="/trusted"), patch.object(
            m.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "identity mismatch")
        ) as process:
            with self.assertRaisesRegex(m.ProbeFailure, "refused"):
                m.confirmed_phone_environment("phone-adb")
            self.assertEqual(process.call_args.args[0], ["phone-adb", "env"])

    def test_full_host_protocol_is_actual_json_and_private(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m.shutil, "which", return_value="/verified/rea"):
            def fake_call(bin, args, env):
                return 0, json.dumps({"ok": True, "normalized_result": {"example": "synthetic"}}).encode(), b""
            with patch.object(m, "execute_rea", side_effect=fake_call) as calls:
                dst = Path(d) / "evidence"
                result = m.run_protocol(execute=True, phone=False, model_file=None,
                    rea_bin="rea", phone_adb="phone-adb", output_directory=dst)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(len(result["evidence"]), 2)
            self.assertEqual(calls.call_count, 2)
            self.assertEqual((dst / "doctor.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(dst.stat().st_mode & 0o777, 0o700)
            self.assertEqual((dst / "summary.json").stat().st_mode & 0o777, 0o600)

    def test_phone_protocol_uses_only_pinned_serial_and_adb(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m.shutil, "which", return_value="/verified/rea"):
            with patch.object(m, "confirmed_phone_environment", return_value={
                "ANDROID_SERIAL": "private-serial:1234", "PHONE_HARNESS_ADB": "/trusted/adb"
            }):
                seen = []
                def fake_call(bin, args, env):
                    seen.append((args, env.get("REA_ADB_PATH")))
                    return 0, b'{"ok":true}', b""
                with patch.object(m, "execute_rea", side_effect=fake_call):
                    result = m.run_protocol(execute=True, phone=True, model_file=None,
                        rea_bin="rea", phone_adb="phone-adb",
                        output_directory=Path(d) / "evidence")
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["phone_accessed"])
            self.assertEqual(seen[2], (("inspect-adb-device", "private-serial:1234"), "/trusted/adb"))
            self.assertEqual(seen[3], (("list-adb-features", "private-serial:1234"), "/trusted/adb"))
            self.assertNotIn("private-serial", json.dumps(result))

    def test_false_ok_and_non_json_fail_before_next_probe(self):
        for payload in [b'{"ok": false}', b"garbage", b'[]']:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as d:
                with patch.object(m.shutil, "which", return_value="/verified/rea"), patch.object(
                    m, "execute_rea", return_value=(0, payload, b"")
                ) as calls:
                    result = m.run_protocol(execute=True, phone=False, model_file=None,
                        rea_bin="rea", phone_adb="phone-adb",
                        output_directory=Path(d) / "evidence")
                    self.assertEqual(result["status"], "FAIL")
                    self.assertEqual(calls.call_count, 1)

    def test_model_artifact_is_scoped_and_no_auto_download(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m.shutil, "which", return_value="/verified/rea"):
            f = Path(d) / "weights.safetensors"
            f.write_bytes(b"\0" * 16)
            with patch.object(m, "execute_rea", return_value=(0, b'{"ok": true}', b"")) as called:
                result = m.run_protocol(execute=True, phone=False, model_file=f,
                    rea_bin="rea", phone_adb="phone-adb",
                    output_directory=Path(d) / "evidence")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(called.call_args.args[1],
                             ("inspect-model-artifact", str(f), "--hash", "full"))

    def test_missing_model_file_rejected_preflight(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m.shutil, "which", return_value="/verified/rea"), patch.object(m, "execute_rea") as called:
            with self.assertRaises(m.ProbeFailure):
                m.run_protocol(execute=True, phone=False,
                    model_file=Path(d) / "not-present.gguf", rea_bin="rea",
                    phone_adb="phone-adb", output_directory=Path(d) / "evidence")
            called.assert_not_called()


if __name__ == "__main__":
    unittest.main()

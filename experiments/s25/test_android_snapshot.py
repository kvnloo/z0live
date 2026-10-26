"""Synthetic S25 hardware-read tests; never access a phone."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import android_snapshot as m
from rea_protocol import ProbeFailure


class HardwareSnapshotTests(unittest.TestCase):
    def test_dry_run_no_adb(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m, "pinned_read") as read:
            r = m.snapshot(execute=False, wrapper="phone-adb", output_dir=Path(d) / "new")
            self.assertEqual(r["status"], "NOT_RUN")
            self.assertEqual(len(r["planned_reads"]), 8)
            read.assert_not_called()

    def test_fails_without_pinned_identity(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            m, "confirmed_phone_environment", side_effect=ProbeFailure("unverified")
        ), patch.object(m, "pinned_read") as read:
            with self.assertRaisesRegex(ProbeFailure, "unverified"):
                m.snapshot(execute=True, wrapper="phone-adb", output_dir=Path(d) / "new")
            read.assert_not_called()

    def test_model_mismatch_aborts_before_all_other_reads(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            m, "confirmed_phone_environment", return_value={"ANDROID_SERIAL":"private","PHONE_HARNESS_ADB":"adb"}
        ), patch.object(m, "pinned_read", return_value=(0, b"NOT-THE-S25\n", b"")) as read:
            with self.assertRaisesRegex(ProbeFailure, "S25 model mismatch"):
                m.snapshot(execute=True, wrapper="phone-adb", output_dir=Path(d) / "new")
            self.assertEqual(read.call_count, 1)
            self.assertFalse((Path(d) / "new").exists())

    def test_partial_snapshot_tracks_unknown_accelerators_and_private_files(self):
        with tempfile.TemporaryDirectory() as d, patch.object(
            m, "confirmed_phone_environment", return_value={"ANDROID_SERIAL":"private","PHONE_HARNESS_ADB":"adb"}
        ):
            calls = [
                (0, b"SM-S938U\n", b""),
                (0, b"Snapdragon 8 Elite\n", b""),
                (0, b"sun\n", b""),
                (0, b"MemTotal: 12582912 kB\nMemAvailable: 4194304 kB\n", b""),
                (0, b"CPU architecture: AArch64\n", b""),
                (1, b"", b"permission denied"),
                (0, b"Thermal Status: 1\n", b""),
                (0, b"level: 70\n", b""),
            ]
            with patch.object(m, "pinned_read", side_effect=calls) as read:
                path = Path(d) / "evidence"
                r = m.snapshot(execute=True, wrapper="phone-adb", output_dir=path)
            self.assertEqual(read.call_count, 8)
            self.assertEqual(r["status"], "PARTIAL")
            self.assertEqual(r["memory"], {"MemTotal_MiB": 12288.0, "MemAvailable_MiB": 4096.0})
            self.assertEqual(r["gpu_inference"], "unknown")
            self.assertEqual(r["npu_inference"], "unknown")
            self.assertEqual(r["cpu_inference"], "unknown")
            self.assertEqual(r["facts"][5]["status"], "UNAVAILABLE")
            self.assertEqual((path / "summary.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((path / "memory.txt").stat().st_mode & 0o777, 0o600)

    def test_meminfo_requires_exact_fields(self):
        self.assertEqual(m.memory_mib(b"MemTotal: 1024 kB\nMemAvailable: 512 kB\n"),
                         {"MemTotal_MiB": 1.0, "MemAvailable_MiB": 0.5})
        self.assertEqual(m.memory_mib(b"MemTotal: unavailable\n"), {})


if __name__ == "__main__":
    unittest.main()

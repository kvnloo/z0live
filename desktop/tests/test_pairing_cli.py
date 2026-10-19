"""Private QR delivery with an isolated Store; never touches deployment credentials."""

import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
from z0live_desktop.core import Store  # noqa: E402

CLI = runpy.run_path(str(ROOT / "bin/z0live"))["main"]
TOKEN = "deterministic-test-pairing-secret"


class PairingCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(CLI.__globals__, Store=lambda: self.store),
            patch.object(sys, "argv", ["z0live", "pair", "test device", *args]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                CLI()
                status = 0
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def assert_private(self, output):
        self.assertNotIn(TOKEN, output)
        self.assertNotIn("z0live://pair?", output)
        self.assertNotIn("Device token:", output)

    @unittest.skipUnless(shutil.which("qrencode"), "qrencode not installed")
    def test_real_terminal_qr_no_plaintext_or_argv_secret(self):
        original = subprocess.run
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            return original(args, **kwargs)

        with (
            patch("z0live_desktop.core.secrets.token_urlsafe", return_value=TOKEN),
            patch("subprocess.run", side_effect=run),
        ):
            status, out, err = self.invoke("--qr")
        self.assertEqual(status, 0)
        self.assertEqual(err, "")
        self.assertIn("\x1b[", out)
        self.assertIn("Scan pairing QR", out)
        self.assert_private(out)
        self.assertNotIn(TOKEN, str(calls[0][0]))
        self.assertIn(TOKEN.encode(), calls[0][1]["input"])
        self.assertIsNotNone(self.store.authenticate(TOKEN))

    @unittest.skipUnless(shutil.which("qrencode"), "qrencode not installed")
    def test_real_png_private_even_with_permissive_umask(self):
        path = Path(self.tmp.name) / "pair.png"
        old_umask = os.umask(0)
        try:
            with patch("z0live_desktop.core.secrets.token_urlsafe", return_value=TOKEN):
                status, out, err = self.invoke("--qr-png", str(path))
        finally:
            os.umask(old_umask)
        self.assertEqual(status, 0)
        self.assertEqual(err, "")
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assert_private(out)
        if shutil.which("zbarimg"):
            decoded = (
                subprocess.run(
                    ["zbarimg", "--quiet", "--raw", str(path)],
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
            self.assertEqual(
                decoded,
                "z0live://pair?url=https%3A%2F%2F0.taila30dc.ts.net%3A8791&token="
                + TOKEN,
            )

    @unittest.skipUnless(shutil.which("qrencode"), "qrencode not installed")
    def test_combined_flags_and_refuse_existing_file_or_symlink(self):
        path = Path(self.tmp.name) / "pair.png"
        with patch("z0live_desktop.core.secrets.token_urlsafe", return_value=TOKEN):
            status, out, err = self.invoke("--qr", "--qr-png", str(path))
        self.assertEqual(status, 0)
        self.assert_private(out + err)
        original = path.read_bytes()
        status, out, err = self.invoke("--qr-png", str(path))
        self.assertEqual(status, 1)
        self.assertEqual(path.read_bytes(), original)
        link = path.with_name("link.png")
        link.symlink_to(path)
        self.assertEqual(self.invoke("--qr-png", str(link))[0], 1)
        self.assertEqual(path.read_bytes(), original)
        devices = json.loads((self.store.directory / "devices.json").read_text())
        self.assertEqual(len(devices), 1)

    def test_missing_encoder_does_not_mint_or_leak(self):
        with (
            patch("shutil.which", return_value=None),
            patch.object(self.store, "pair") as pair,
        ):
            status, out, err = self.invoke("--qr")
        self.assertEqual(status, 2)
        pair.assert_not_called()
        self.assert_private(out + err)

    def test_encoder_failure_revokes_and_removes_partial_png(self):
        path = Path(self.tmp.name) / "pair.png"
        with (
            patch("shutil.which", return_value="qrencode"),
            patch("z0live_desktop.core.secrets.token_urlsafe", return_value=TOKEN),
            patch(
                "subprocess.run", side_effect=subprocess.CalledProcessError(1, [TOKEN])
            ),
        ):
            status, out, err = self.invoke("--qr-png", str(path))
        self.assertEqual(status, 1)
        self.assert_private(out + err)
        self.assertIsNone(self.store.authenticate(TOKEN))
        self.assertFalse(path.exists())

    def test_legacy_output_preserved_only_without_qr_flags(self):
        with patch("z0live_desktop.core.secrets.token_urlsafe", return_value=TOKEN):
            status, out, err = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(err, "")
        self.assertIn("Device token: " + TOKEN, out)
        self.assertIn("Pairing link: z0live://pair?", out)

    def test_store_unique_hash_only_and_failed_delivery_revocation(self):
        one, two = self.store.pair("same name"), self.store.pair("same name")
        self.assertNotEqual(one, two)
        before = self.store.authenticate(two)
        self.store.revoke_token(one)
        self.store.revoke_token(one)
        self.assertIsNone(self.store.authenticate(one))
        self.assertEqual(self.store.authenticate(two), before)
        path = self.store.directory / "devices.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(one, path.read_text())
        self.assertNotIn(two, path.read_text())


if __name__ == "__main__":
    unittest.main()

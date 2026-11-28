"""Tests for bin/phone-screen-safe. Canary values are synthetic and built at runtime."""
import importlib.machinery
import importlib.util
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path


BIN = Path(__file__).resolve().parents[1] / "bin" / "phone-screen-safe"
loader = importlib.machinery.SourceFileLoader("phone_screen_safe", str(BIN))
spec = importlib.util.spec_from_loader("phone_screen_safe", loader)
pss = importlib.util.module_from_spec(spec)
loader.exec_module(pss)

# Synthetic canaries, assembled so the source holds no literal secret.
SHA = "c0ffee" * 6 + "ab12"                       # 40 hex chars, like a sha token
GH = "gh" + "p_" + "Zq9" * 12                      # github-style token
JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIx" + "." + "SflKxwRJSMeK"


class PhoneScreenSafeTests(unittest.TestCase):
    def make_temp_dir(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def test_mask_removes_value(self):
        for value in (SHA, GH, JWT, "AKIA" + "Q" * 16, "owner@example.com",
                      "token: hunter2x", "(555) 123-4567", "4111 1111 1111 1111"):
            with self.subTest(value=value):
                out = pss.mask(f"label {value} tail")
                self.assertNotIn(value, out)
                self.assertIn(pss.REDACTED, out)


    def test_plain_ui_text_kept(self):
        for value in ("Settings", "Connected to Wi-Fi", "Send message", "Version 15", "ChatGPT", "Worked for 2m 3s"):
            with self.subTest(value=value):
                self.assertEqual(pss.mask(value), value)


    def test_xml_password_field_and_bounds(self):
        xml = ('<?xml version="1.0"?><hierarchy><node text="Token" bounds="[0,0][10,10]" password="false"/>'
               f'<node text="{SHA}" bounds="[10,20][300,60]" password="false"/>'
               '<node text="secretpw" bounds="[0,100][300,140]" password="true"/></hierarchy>')
        out, boxes = pss.redact_xml(xml)
        assert SHA not in out and "secretpw" not in out and 'text="Token"' in out
        assert (10, 20, 300, 60) in boxes and (0, 100, 300, 140) in boxes


    def test_sensitive_packages(self):
        assert pss.sensitive_package("com.x8bit.bitwarden")
        assert pss.sensitive_package("com.google.android.apps.authenticator2")
        assert not pss.sensitive_package("com.openai.chatgpt")


    def test_slm_refuses_remote_host(self):
        with self.assertRaises(SystemExit):
            pss.slm_flag_lines(["x"], url="http://example.com:11434")


    @unittest.skipUnless(shutil.which("tesseract"), "tesseract missing")
    def test_image_canary_is_blacked_out(self):
        tmp_path = self.make_temp_dir()
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self.skipTest("Pillow missing")
        img = Image.new("RGB", (1400, 300), "white")
        try:
            font = ImageFont.truetype("DejaVuSansMono.ttf", 36)
        except OSError:
            font = ImageFont.load_default(size=36)
        d = ImageDraw.Draw(img)
        d.text((20, 40), "Settings", fill="black", font=font)
        d.text((20, 140), SHA, fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        out, code = pss.redact_png(buf.getvalue())
        assert code == 0
        p = tmp_path / "o.png"
        p.write_bytes(out)
        text = " ".join(w["text"] for w in pss.ocr_words(Image.open(p)))
        assert SHA not in text and "Settings" in text
        assert pss.scan([str(p)]) == 0


    @patch.object(pss.shutil, "which", return_value=None)
    def test_fail_closed_without_tesseract(self, _which):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow missing")
        buf = io.BytesIO()
        Image.new("RGB", (400, 400), "white").save(buf, "PNG")
        out, code = pss.redact_png(buf.getvalue())
        assert code == pss.EXIT_FAIL_CLOSED
        assert Image.open(io.BytesIO(out)).getpixel((399, 399)) == (0, 0, 0)


    def test_scan_flags_text_leak(self):
        p = self.make_temp_dir() / "log.txt"
        p.write_text(f"evidence {GH}\n")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(pss.scan([str(p)]), 1)
        self.assertNotIn(GH, output.getvalue())

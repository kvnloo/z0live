"""Tests for bin/phone-screen-safe. Canary values are synthetic and built at runtime."""
import importlib.machinery
import importlib.util
import io
import shutil
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin" / "phone-screen-safe"
loader = importlib.machinery.SourceFileLoader("phone_screen_safe", str(BIN))
spec = importlib.util.spec_from_loader("phone_screen_safe", loader)
pss = importlib.util.module_from_spec(spec)
loader.exec_module(pss)

# Synthetic canaries, assembled so the source holds no literal secret.
SHA = "c0ffee" * 6 + "ab12"                       # 40 hex chars, like a sha token
GH = "gh" + "p_" + "Zq9" * 12                      # github-style token
JWT = "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIx" + "." + "SflKxwRJSMeK"


@pytest.mark.parametrize("value", [SHA, GH, JWT, "AKIA" + "Q" * 16, "owner@example.com",
                                   "token: hunter2x", "(555) 123-4567", "4111 1111 1111 1111"])
def test_mask_removes_value(value):
    out = pss.mask(f"label {value} tail")
    assert value not in out and pss.REDACTED in out


@pytest.mark.parametrize("text", ["Settings", "Connected to Wi-Fi", "Send message",
                                  "Version 15", "ChatGPT", "Worked for 2m 3s"])
def test_plain_ui_text_kept(text):
    assert pss.mask(text) == text


def test_xml_password_field_and_bounds():
    xml = ('<?xml version="1.0"?><hierarchy><node text="Token" bounds="[0,0][10,10]" password="false"/>'
           f'<node text="{SHA}" bounds="[10,20][300,60]" password="false"/>'
           '<node text="secretpw" bounds="[0,100][300,140]" password="true"/></hierarchy>')
    out, boxes = pss.redact_xml(xml)
    assert SHA not in out and "secretpw" not in out and 'text="Token"' in out
    assert (10, 20, 300, 60) in boxes and (0, 100, 300, 140) in boxes


def test_sensitive_packages():
    assert pss.sensitive_package("com.x8bit.bitwarden")
    assert pss.sensitive_package("com.google.android.apps.authenticator2")
    assert not pss.sensitive_package("com.openai.chatgpt")


def test_slm_refuses_remote_host():
    with pytest.raises(SystemExit):
        pss.slm_flag_lines(["x"], url="http://example.com:11434")


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract missing")
def test_image_canary_is_blacked_out(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
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


def test_fail_closed_without_tesseract(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(pss.shutil, "which", lambda _: None)
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buf, "PNG")
    out, code = pss.redact_png(buf.getvalue())
    assert code == pss.EXIT_FAIL_CLOSED
    assert Image.open(io.BytesIO(out)).getpixel((399, 399)) == (0, 0, 0)


def test_scan_flags_text_leak(tmp_path, capsys):
    p = tmp_path / "log.txt"
    p.write_text(f"evidence {GH}\n")
    assert pss.scan([str(p)]) == 1
    assert GH not in capsys.readouterr().out

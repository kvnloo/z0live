# Screen redaction for agents

Agents that look at a phone screen must not receive secrets or PII that happen to be on it:
tokens, sha-like hashes, API keys, one-time codes, passwords, emails, phone numbers, card
numbers, IP addresses and similar. `bin/phone-screen-safe` is the single gate.

## Law

1. An agent reads the screen only through `phone-screen-safe` (CLI) or its Python API
   (`safe_screenshot`, `safe_hierarchy`, `redact_png`, `redact_xml`). Raw `adb exec-out screencap`,
   `uiautomator dump`, `d.screenshot()` or `d.dump_hierarchy()` output is never written to a
   file, log or transcript an agent can read.
2. Raw pixels stay in memory. Only the redacted frame is written.
3. Fail closed. When OCR is unavailable or fails, the frame is fully black and the exit code
   is 3. When no hierarchy can be parsed, the dump is an empty placeholder with exit code 3.
4. Sensitive apps (password managers, authenticators, banks, wallets) give a fully black frame.
5. The optional `--slm` layer uses a local Ollama model on localhost only. Screen text never
   leaves the host. The tool refuses a non-localhost model URL.
6. `phone-screen-safe scan PATH...` exits 1 when a high-confidence secret pattern remains in a
   file or in the OCR of an image. It prints rule names only, never the value.

## Usage

```bash
phone-screen-safe shot -s SERIAL out.png          # screencap -> redact -> out.png
phone-screen-safe dump -s SERIAL out.xml          # hierarchy with masked values
phone-screen-safe shot -s SERIAL out.png --slm    # add the local SLM layer (slower)
uv run --with uiautomator2 --with pillow phone-screen-safe dump -s SERIAL
                                                  # uiautomator2 fallback for animated screens
```

```python
from phone_screen_safe import safe_screenshot, safe_hierarchy   # or load bin/phone-screen-safe
safe_screenshot(d, "evidence.png")
xml = safe_hierarchy(d)
```

## Layers

| Layer | What it catches |
|---|---|
| Hierarchy | `password="true"` fields, and text / content-desc / hint values that match a rule; their bounds are blacked out in the image too |
| OCR (tesseract) | Secret and PII rules plus an entropy test for long random strings, per word and per line |
| `--slm` (local qwen3 4B) | Free-form PII the rules miss, such as street addresses or private message text |
| Package policy | Whole-frame blackout for sensitive apps |

Limits: OCR can miss very small or stylised text. The hierarchy layer covers native text
views; for web views and canvases the OCR layer is the guard. Use `--slm` for screens that
can show free-form personal data.

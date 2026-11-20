"""Transcribe every human talkspurt of the Dessa reference calls with the local LFM2.5-Audio model (Track C).

The transcript of a talkspurt feeds floor_data's text features once it would exist in a streaming setup (talkspurt
end + ASR_LAG). The model runs on the CPU (-ngl 0) in a private llama-liquid-audio-server on a random local port;
nothing leaves the machine. Output: ~/.local/share/z0live/orch/asr/NAME.json = [[start, end, text], ...] (private,
never in git). Calls already transcribed are skipped, so an interrupted run resumes.

  python bench/floor_asr.py [--threads 6]
"""
import argparse
import base64
import io
import json
import socket
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import floor_data as fd  # noqa: E402

MODELS = Path.home() / ".local/share/z0live/models/lfm25-audio"
RUNNER = MODELS / "llama-liquid-audio-macos-arm64/llama-liquid-audio-macos-arm64/llama-liquid-audio-server"
LIQUID = "LFM2.5-Audio-1.5B-Q4_0.gguf"
OUT = Path.home() / ".local/share/z0live/orch/asr"


def wav_b64(x):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def asr(url, x):
    body = {"messages": [{"role": "system", "content": "Perform ASR."},
                         {"role": "user", "content": [{"type": "input_audio",
                                                       "input_audio": {"format": "wav", "data": wav_b64(x)}}]}],
            "stream": True, "max_tokens": 256, "reset_context": True}  # the server only streams (400 otherwise)
    req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=120) as r:
        for line in r:
            data = line[5:].strip() if line.startswith(b"data:") else b""
            if not data or data == b"[DONE]":
                continue
            out.append(json.loads(data)["choices"][0]["delta"].get("content") or "")
    return "".join(out).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=6)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    todo = [n for n in fd.names() if not (OUT / f"{n}.json").exists()]
    if not todo:
        print("all calls transcribed")
        return
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url, log = f"http://127.0.0.1:{port}", OUT / "server.log"
    args = [RUNNER, "--host", "127.0.0.1", "--port", str(port), "--offline", "-c", "2048", "-ngl", "0",
            "-t", str(a.threads), "-s", "42", "-m", MODELS / LIQUID, "-mm", MODELS / f"mmproj-{LIQUID}",
            "-mv", MODELS / f"vocoder-{LIQUID}", "--tts-speaker-file", MODELS / f"tokenizer-{LIQUID}"]
    with log.open("wb") as stream:
        proc = subprocess.Popen([str(x) for x in args], stdout=stream, stderr=stream, cwd=RUNNER.parent)
    try:
        deadline = time.time() + 120
        while b"Server ready at" not in log.read_bytes():
            if proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f"ASR server did not start; see {log}")
            time.sleep(0.2)
        for name in todo:
            t0, c, human = time.time(), fd.load(name), fd.tracks(name)[0]
            rows = [[s, e, asr(url, human[int(s * 16000): int(e * 16000)])] for s, e in c.human]
            (OUT / f"{name}.json").write_text(json.dumps(rows))
            print(f"{name}: {len(rows)} talkspurts, {sum(bool(r[2]) for r in rows)} with text, "
                  f"{time.time() - t0:.0f}s", flush=True)
    finally:
        proc.terminate()
        proc.wait(10)


if __name__ == "__main__":
    main()

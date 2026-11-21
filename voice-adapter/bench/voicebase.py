#!/usr/bin/env python3
"""Run the Studio's bundled Liquid server against synthetic fixtures, with no playback."""
import argparse
from array import array
import base64
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import socket
from statistics import median
import subprocess
import sys
import time
import wave

TTS = (
    "The kettle is ready now.",
    "Please leave the blue notebook beside the window.",
    "The last train leaves before sunset, so bring your ticket to the station early.",
    "I thought the shop was closed, but the lights were still on.",
    "After lunch, we can walk to the river and watch the boats pass under the bridge.",
    "The garden smells like mint after the rain.",
    "When the clouds moved away, the evening sun lit up every little pool of water along the path beside our house.",
    "Could you say that again a little more slowly?",
    "She set the warm bread on the table and called everyone in from the yard.",
    "The map showed a narrow trail through the woods, but we stayed on the wide gravel road until we reached the old stone gate.",
)
CHAT = (
    "What is the capital of France?",
    "What color do you get by mixing red and white paint?",
    "Which planet is known as the red planet?",
    "What do bees make from nectar?",
    "How many legs does a spider have?",
    "What is the opposite of north?",
    "Which animal is famous for its long neck?",
    "What musical instrument has black and white keys?",
    "In what season do most leaves fall from the trees?",
    "What is the name of the ocean between California and Japan?",
)
PROMPTS = dict(asr="Perform ASR.", tts="Perform TTS. Use the US male voice.",
               chat="Respond with interleaved text and audio.")
MODEL = "LFM2.5-Audio-1.5B-Q4_0.gguf"
RUNNER = "llama-liquid-audio-macos-arm64"


def words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'"))


def edits(ref, hyp):
    row = list(range(len(hyp) + 1))
    for i, a in enumerate(ref, 1):
        nxt = [i]
        for j, b in enumerate(hyp, 1):
            nxt.append(min(row[j] + 1, nxt[-1] + 1, row[j-1] + (a != b)))
        row = nxt
    return row[-1]


def synth(path, text, voice):
    aiff = path.with_suffix(".aiff")
    subprocess.run(["/usr/bin/say", "-v", voice, "-r", "175", "-o", aiff, text], check=True)
    subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff, path], check=True)
    aiff.unlink()
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2) or not wav.getnframes():
            raise RuntimeError(f"Bad fixture: {path}")
        return wav.getnframes() / wav.getframerate()


@contextmanager
def server(models, log):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    binary = models / RUNNER / RUNNER / "llama-liquid-audio-server"
    args = [binary, "--host", "127.0.0.1", "--port", str(port), "--offline",
            "-c", "2048", "-ngl", "all", "-s", "42", "--perf", "-m", models / MODEL,
            "-mm", models / ("mmproj-" + MODEL), "-mv", models / ("vocoder-" + MODEL),
            "--tts-speaker-file", models / ("tokenizer-" + MODEL)]
    with log.open("wb") as stderr:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LLAMA_", "GGML_"))}
        proc = subprocess.Popen(args, stdout=stderr, stderr=stderr, cwd=binary.parent, env=env)
        peak = {}
        try:
            deadline = time.monotonic() + 60
            with log.open("rb") as stdout:
                ready = f"Server ready at http://127.0.0.1:{port}".encode()
                while not any(ready in line for line in stdout):
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"Runner did not start: {log}")
                    time.sleep(0.05)
            yield port, peak
            if proc.poll() is not None:
                raise RuntimeError(f"Runner exited early: {log}")
        finally:
            if proc.returncode is None:
                proc.terminate()
                deadline = time.monotonic() + 5
                while True:
                    pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
                    if pid:
                        break
                    if time.monotonic() > deadline:
                        proc.kill()
                        pid, status, usage = os.wait4(proc.pid, 0)
                        break
                    time.sleep(0.01)
                proc.returncode = os.waitstatus_to_exitcode(status)
                peak["peak_rss_bytes"] = usage.ru_maxrss


def request(port, mode, text, src, dst):
    user = text if src is None else [{"type": "input_audio", "input_audio": {
        "data": base64.b64encode(src.read_bytes()).decode(), "format": "wav"}}]
    body = json.dumps(dict(model="", stream=True, reset_context=True, max_tokens=512, messages=[
        dict(role="system", content=PROMPTS[mode]), dict(role="user", content=user),
    ])).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    text_parts, pcm = [], array("f")
    rate = first_text = first_audio = last_audio = finish = None
    done = False
    try:
        start = time.monotonic()
        conn.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200 or not response.getheader("Content-Type", "").startswith("text/event-stream"):
            raise RuntimeError(f"Unexpected HTTP response: {response.status} {response.read(512)!r}")
        for line in response:
            elapsed = time.monotonic() - start
            if not line.startswith(b"data: "):
                continue
            payload = line[6:].strip()
            if payload == b"[DONE]":
                done = True
                break
            event = json.loads(payload)
            if "error" in event:
                raise RuntimeError(event["error"])
            choice = event["choices"][0]
            if choice["finish_reason"] is not None:
                if choice["finish_reason"] != "stop":
                    raise RuntimeError(f"Unfinished turn: {choice['finish_reason']}")
                finish = elapsed
            delta = choice["delta"]
            if content := delta.get("content"):
                if first_text is None:
                    first_text = elapsed
                text_parts.append(content)
            if chunk := delta.get("audio_chunk"):
                chunk_rate = chunk["sample_rate"]
                if chunk_rate != (rate or chunk_rate):
                    raise RuntimeError("Sample rate changed midstream")
                rate = chunk_rate
                data = base64.b64decode(chunk["data"], validate=True)
                if data:
                    if first_audio is None:
                        first_audio = elapsed
                    last_audio = elapsed
                    pcm.frombytes(data)
    finally:
        conn.close()
    if finish is None or not done:
        raise RuntimeError("Stream ended without stop and [DONE]")
    result = dict(stop_s=finish, first_text_s=first_text, text="".join(text_parts).strip())
    if mode == "asr":
        if pcm or not result["text"]:
            raise RuntimeError(f"Unexpected ASR output: {result}")
        result.update(ref_words=len(words(text)), word_edits=edits(words(text), words(result["text"])))
    else:
        if rate != 24000 or not pcm or not all(-1.01 <= x <= 1.01 for x in pcm) or max(map(abs, pcm)) < 0.001:
            raise RuntimeError(f"Bad audio: {rate=} {len(pcm)=}")
        with wave.open(str(dst), "wb") as wav:
            wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            wav.writeframes(array("h", (round(max(-1, min(x, 1)) * 32767) for x in pcm)).tobytes())
        duration = len(pcm) / rate
        result.update(first_audio_s=first_audio, last_audio_s=last_audio,
                      audio_s=duration, rtf=finish / duration)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path, help="new output directory outside this worktree")
    parser.add_argument("--models", default=Path.home() / ".local/share/z0live/models/lfm25-audio", type=Path)
    parser.add_argument("--smoke", action="store_true", help="run one case per mode after warm-up")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("this benchmark requires macOS")
    out = args.out.resolve()
    if out.is_relative_to(Path(__file__).resolve().parents[2]):
        parser.error("output must be outside the worktree")
    out.mkdir(parents=True, exist_ok=False)
    results = {"numbers": "measured (including summaries calculated from observations)", "modes": {}}
    for mode, cases in [("asr", TTS + CHAT), ("tts", TTS), ("chat", CHAT)]:
        cases = cases[:1] if args.smoke else cases
        fixtures = []
        for i, text in enumerate(("Could you greet the morning visitors?", *cases)):
            path = out / f"{mode}-{i:02d}-input.wav" if mode != "tts" else None
            duration = synth(path, text, ("Samantha", "Alex")[i % 2]) if path else None
            fixtures.append((text, path, duration))
        rows = []
        with server(args.models.resolve(), out / f"{mode}.log") as (port, peak):
            for i, (text, path, duration) in enumerate(fixtures):
                got = request(port, mode, text, path, out / f"{mode}-{i:02d}.wav")
                if i:
                    rows.append(dict(case=i, input=text, input_s=duration, **got))
        summary = {"peak_rss_bytes": peak["peak_rss_bytes"]}
        for metric in ("stop_s", "first_text_s", "first_audio_s", "audio_s", "rtf"):
            values = [r[metric] for r in rows if r.get(metric) is not None]
            if values:
                summary[metric] = dict(n=len(values), median=median(values), min=min(values), max=max(values))
        if mode == "asr":
            summary["word_edits"] = sum(r["word_edits"] for r in rows)
            summary["ref_words"] = sum(r["ref_words"] for r in rows)
            summary["wer"] = summary["word_edits"] / summary["ref_words"]
        results["modes"][mode] = dict(summary=summary, cases=rows)
        (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(mode, json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

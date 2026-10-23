#!/usr/bin/env python3
"""Isolated synthetic-audio voice benchmark. Never opens microphone or speakers.

Optional model dependencies are imported only by the selected adapter. PCM sink
latencies are not physical playback measurements. Historical controls retain
provenance and are never pooled with new local measurements.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time

SCENARIOS = {
    "greet": ("Hi there, how are you doing?", None),
    "knowledge": ("Quick question. What's the capital of Australia?", r"\bcanberra\b"),
    "phone": ("Open Spotify.", r"\b(confirm|approval|cannot|can't|request|ask|open|opening)\b"),
    "work": ("Can you check on my computer which git branch the z0live repo is on right now?", r"\b(check|cannot|can't|access|request|ask|need)\b"),
    "barge": ("Tell me a long story about a lighthouse keeper. Make it at least a minute long.", None),
    "interrupt": ("Wait, stop. What is two plus two?", r"\b(four|4)\b"),
    "correction": ("Check the z0live branch, no, do not change it. Just tell me its name.", r"\b(check|name|cannot|can't|access|request|ask|need)\b"),
    "cancel": ("Cancel that task. Do not run any commands.", r"\b(cancel|stop|won't|will not|no commands)\b"),
}


def words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def word_errors(reference, hypothesis):
    a, b = words(reference), words(hypothesis)
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        new = [i]
        for j, y in enumerate(b, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (x != y)))
        row = new
    return row[-1], len(a)


def percentile(values, p):
    s = sorted(v for v in values if v is not None)
    return s[min(len(s) - 1, math.ceil(p * len(s)) - 1)] if s else None


def gpu():
    line = subprocess.check_output([
        "nvidia-smi", "--query-gpu=memory.used,memory.free,utilization.gpu,power.draw",
        "--format=csv,noheader,nounits"], text=True, timeout=5).strip().splitlines()[0]
    vals = line.split(",")
    return {"gpu_used_mib": float(vals[0]), "gpu_free_mib": float(vals[1]),
            "gpu_pct": float(vals[2]), "gpu_watts": float(vals[3])}


class Resources:
    def __init__(self, reserve):
        self.reserve = reserve
        self.samples = []
        self.stop = threading.Event()
        self.error = None

    def __enter__(self):
        import psutil
        if gpu()["gpu_free_mib"] < self.reserve:
            raise RuntimeError("GPU admission failed before allocation")
        self.process = psutil.Process()
        self.process.cpu_percent()
        psutil.cpu_percent()
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()
        return self

    def _sample(self):
        import psutil
        while not self.stop.is_set():
            try:
                sample = {"t": time.monotonic(), **gpu(),
                          "cpu_host_pct": psutil.cpu_percent(),
                          "cpu_process_pct": self.process.cpu_percent(),
                          "rss_mib": self.process.memory_info().rss / 1048576}
                self.samples.append(sample)
                if sample["gpu_free_mib"] < self.reserve:
                    self.error = "GPU headroom breached; stop requested"
                    self.stop.set()
            except Exception as exc:
                self.error = str(exc)
                self.stop.set()
            self.stop.wait(0.5)

    def check(self):
        if self.error:
            raise RuntimeError(self.error)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=6)


class WhisperASR:
    def __init__(self, model):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model, device="cuda", compute_type="int8", cpu_threads=2, num_workers=1)

    def transcribe(self, audio, rate):
        import numpy as np
        from scipy.signal import resample_poly
        pcm = resample_poly(audio, 16000, rate).astype(np.float32)
        segments, _ = self.model.transcribe(pcm, language="en", beam_size=1, vad_filter=False)
        return " ".join(s.text.strip() for s in segments)


class KyutaiASR:
    def __init__(self, model):
        import torch
        from moshi.models import loaders, LMGen
        self.torch = torch
        self.info = loaders.CheckpointInfo.from_hf_repo(model)
        self.mimi = self.info.get_mimi(device="cuda")
        self.tokenizer = self.info.get_text_tokenizer()
        self.lm = self.info.get_moshi(device="cuda", dtype=torch.bfloat16)
        self.generator = LMGen(self.lm, use_sampling=False)
        self.frame = int(self.mimi.sample_rate / self.mimi.frame_rate)

    def transcribe(self, audio, rate):
        import numpy as np
        from scipy.signal import resample_poly
        torch = self.torch
        config = self.info.stt_config
        pcm = resample_poly(audio, 24000, rate).astype(np.float32)
        prefix = int(config.get("audio_silence_prefix_seconds", 0) * 24000)
        suffix = int((config.get("audio_delay_seconds", 0) + 1) * 24000)
        pcm = np.pad(pcm, (prefix, suffix))
        tensor = torch.from_numpy(pcm).reshape(1, 1, -1).to("cuda")
        tokens = []
        with torch.inference_mode(), self.mimi.streaming(1), self.generator.streaming(1):
            first = True
            for chunk in tensor.split(self.frame, dim=-1):
                if chunk.shape[-1] != self.frame:
                    break
                codes = self.mimi.encode(chunk)
                if first:
                    self.generator.step(codes)
                    first = False
                result = self.generator.step(codes)
                if result is not None:
                    token = int(result[0, 0, 0])
                    if token not in (0, 3):
                        tokens.append(token)
        return self.tokenizer.decode(tokens)


class LocalSLM:
    def __init__(self, model):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            model, local_files_only=True, torch_dtype=torch.float16).to("cuda").eval()

    def answer(self, text):
        prompt = self.tokenizer.apply_chat_template([
            {"role": "system", "content": "You are a concise voice assistant. Reply in one short sentence. You have no tools or computer access. Never claim you performed an action. For tasks say you need to ask the authorized agent. Answer simple facts directly. No markdown."},
            {"role": "user", "content": text}], tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = self.tokenizer(prompt, return_tensors="pt").to("cuda")
        with self.torch.inference_mode():
            result = self.model.generate(**inputs, max_new_tokens=64, do_sample=False,
                                         pad_token_id=self.tokenizer.eos_token_id)
        return self.tokenizer.decode(result[0, inputs.input_ids.shape[1]:], skip_special_tokens=True)


class KokoroTTS:
    def __init__(self):
        from kokoro import KPipeline
        self.pipeline = KPipeline(lang_code="a", device="cpu", repo_id="hexgrad/Kokoro-82M")
        self.rate = 24000

    def chunks(self, text):
        for _, _, audio in self.pipeline(text, voice="af_heart", speed=1):
            yield audio.numpy()


class PocketTTS:
    def __init__(self):
        from pocket_tts import TTSModel
        self.model = TTSModel.load_model()
        self.voice = self.model.get_state_for_audio_prompt("alba")
        self.rate = self.model.sample_rate

    def chunks(self, text):
        for audio in self.model.generate_audio_stream(self.voice, text):
            yield audio.detach().cpu().numpy()


def prepare(args):
    """Reuse the exact existing six acoustic utterances, with two explicit additions."""
    args.audio.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, (text, _) in SCENARIOS.items():
        output = args.audio / (name + ".wav")
        subprocess.run([args.piper, "-m", args.voice, "-f", str(output)], input=text,
                       text=True, check=True, timeout=60)
    manifest = {"schema": "z0live.voice_bench_fixtures.v1", "source": "desktop/tests/make_test_speech.sh",
                "scope": "synthetic desktop only; no device microphone or speaker", "cases": []}
    for name, (text, expected) in SCENARIOS.items():
        wav = args.audio / (name + ".wav")
        manifest["cases"].append({"id": name, "file": wav.name, "reference": text,
                                  "answer_regex": expected, "sha256": hashlib.sha256(wav.read_bytes()).hexdigest()})
    (args.audio / "manifest.json").write_text(json.dumps(manifest, indent=2))


def summarize(rows):
    complete = [r for r in rows if r.get("first_pcm_ms") is not None]
    errors = sum(r.get("wer_errors", 0) for r in rows)
    denominator = sum(r.get("wer_words", 0) for r in rows)
    scored = [r["answer_keyword_pass"] for r in rows if r.get("answer_keyword_pass") is not None]
    return {"n": len(rows), "complete_n": len(complete),
            "first_pcm_p50_ms": percentile([r.get("first_pcm_ms") for r in rows], .5),
            "first_pcm_p95_ms": percentile([r.get("first_pcm_ms") for r in rows], .95),
            "wer_pct": 100 * errors / denominator if denominator else None,
            "keyword_pass_pct": 100 * sum(scored) / len(scored) if scored else None,
            "task_correctness": None, "task_correctness_status": "keyword rubric only; no tool/effect receipts or human grading",
            "barge_stop_ms": None, "barge_status": "not qualified: this batch cascade does not implement duplex playback",
            "provider_billed_usd_per_hour": 0, "total_cost_usd_per_hour": None,
            "cost_status": "no provider calls; electricity/amortization not measured",
            "release_gate": "PARTIAL", "measurement": "batch-EOS to generated PCM sink, not acoustic playback"}


def run(args):
    import numpy as np
    import soundfile as sf
    import torch
    torch.set_num_threads(2)
    torch.set_num_interop_threads(2)
    manifest = json.loads((args.audio / "manifest.json").read_text())
    for case in manifest["cases"]:
        if hashlib.sha256((args.audio / case["file"]).read_bytes()).hexdigest() != case["sha256"]:
            raise ValueError("fixture hash mismatch: " + case["id"])
    rows = []
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with Resources(args.reserve_mib) as resources:
        start = time.monotonic()
        asr = KyutaiASR(args.asr_model) if args.candidate == "kyutai-pocket" else WhisperASR(args.asr_model)
        resources.check()
        slm = LocalSLM(args.slm_model)
        resources.check()
        tts = PocketTTS() if args.candidate == "kyutai-pocket" else KokoroTTS()
        load_ms = (time.monotonic() - start) * 1000
        for repeat in range(args.runs):
            for case in manifest["cases"]:
                resources.check()
                audio, rate = sf.read(args.audio / case["file"], dtype="float32")
                row = {"candidate": args.candidate, "case": case["id"], "repeat": repeat, "error": None}
                t0 = time.monotonic()
                try:
                    transcript = asr.transcribe(audio, rate)
                    t1 = time.monotonic()
                    resources.check()
                    answer = slm.answer(transcript)
                    t2 = time.monotonic()
                    chunks = []
                    first = None
                    for chunk in tts.chunks(answer):
                        resources.check()
                        chunk = np.asarray(chunk).reshape(-1)
                        # Same RMS 0.01 as the existing acoustic rig; no physical playout implied.
                        if first is None and len(chunk) and np.sqrt(np.mean(chunk ** 2)) > .01:
                            first = time.monotonic()
                        chunks.append(chunk)
                    if chunks:
                        sf.write(args.output / f'{repeat}-{case["id"]}.wav', np.concatenate(chunks), tts.rate)
                    row.update({"transcript": transcript, "answer": answer,
                                "asr_ms": (t1 - t0) * 1000, "slm_ms": (t2 - t1) * 1000,
                                "tts_first_ms": (first - t2) * 1000 if first else None,
                                "first_pcm_ms": (first - t0) * 1000 if first else None,
                                "answer_keyword_pass": bool(re.search(case["answer_regex"], answer, re.I)) if case["answer_regex"] else None})
                    row["wer_errors"], row["wer_words"] = word_errors(case["reference"], transcript)
                except Exception as exc:
                    row.update(error=type(exc).__name__ + ": " + str(exc), first_pcm_ms=None,
                               wer_errors=len(words(case["reference"])), wer_words=len(words(case["reference"])))
                rows.append(row)
                (args.output / "trials.json").write_text(json.dumps(rows, indent=2))
                print(json.dumps(row), flush=True)
    summary = summarize(rows)
    summary.update(candidate=args.candidate, load_ms=load_ms, asr_model=args.asr_model, slm_model=args.slm_model,
                   gpu_total_peak_mib=max(s["gpu_used_mib"] for s in resources.samples),
                   gpu_free_min_mib=min(s["gpu_free_mib"] for s in resources.samples),
                   cpu_host_p95_pct=percentile([s["cpu_host_pct"] for s in resources.samples], .95))
    (args.output / "resources.json").write_text(json.dumps(resources.samples))
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


def probe_moshi(args):
    """Bounded upstream offline probe; --bench explicitly disables SDL I/O."""
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [str(args.binary), "--bench", "-r", str(args.model_root),
               "-c", str(args.context), "--threads", "2", "-s", "44"]
    with Resources(args.reserve_mib) as resources:
        started = time.monotonic()
        with (args.output / "runtime.log").open("w") as log:
            process = subprocess.Popen(command, cwd=args.binary.parent, stdout=log,
                                       stderr=subprocess.STDOUT)
            try:
                while True:
                    resources.check()
                    if time.monotonic() - started > args.timeout:
                        raise TimeoutError("Moshi probe exceeded bounded duration")
                    try:
                        code = process.wait(timeout=.5)
                        break
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    text = (args.output / "runtime.log").read_text()
    result = {"candidate": "moshi-q4", "command": command, "exit_code": code,
              "elapsed_s": time.monotonic() - started, "context_frames": args.context,
              "measurement": "upstream 125-frame silence throughput, SDL disabled",
              "first_pcm_p50_ms": None, "first_pcm_p95_ms": None, "wer_pct": None,
              "barge_stop_ms": None, "task_correctness": None, "release_gate": "PARTIAL",
              "gpu_total_peak_mib": max(s["gpu_used_mib"] for s in resources.samples),
              "gpu_free_min_mib": min(s["gpu_free_mib"] for s in resources.samples),
              "cpu_host_p95_pct": percentile([s["cpu_host_pct"] for s in resources.samples], .95)}
    # Preserve upstream metrics verbatim rather than guessing their regex/version.
    result["runtime_tail"] = text[-4000:]
    (args.output / "resources.json").write_text(json.dumps(resources.samples))
    (args.output / "summary.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if code:
        raise SystemExit(code)


def export(args):
    """Export plot coordinates without asserting dominance across media boundaries."""
    columns = ["candidate", "measurement", "n", "complete_n", "first_pcm_p50_ms",
               "first_pcm_p95_ms", "first_audio_p50_ms", "first_audio_p95_ms",
               "barge_stop_ms", "barge_p50_ms", "barge_p95_ms", "wer_pct",
               "keyword_pass_pct", "task_correctness", "gpu_total_peak_mib",
               "gpu_free_min_mib", "cpu_host_p95_pct", "provider_billed_usd_per_hour",
               "total_cost_usd_per_hour", "cost_status", "release_gate"]
    records = []
    for path in args.summaries:
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or not isinstance(record.get("candidate"), str):
            raise ValueError("invalid summary: " + str(path))
        records.append(record)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.output / "pareto.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    (args.output / "plot-data.json").write_text(json.dumps({
        "schema": "z0live.voice_pareto_plot.v1",
        "x": "first_pcm_p50_ms or first_audio_p50_ms; do not pool endpoints",
        "y": "wer_pct", "size": "gpu_total_peak_mib", "records": records,
        "promotion": "NONE: historical acoustic control, batch PCM and throughput are separate cohorts",
    }, indent=2))
    print(json.dumps(records, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    fixture = subs.add_parser("prepare")
    fixture.add_argument("--piper", required=True)
    fixture.add_argument("--voice", required=True)
    fixture.add_argument("--audio", type=Path, required=True)
    fixture.set_defaults(func=prepare)
    bench = subs.add_parser("run")
    bench.add_argument("--candidate", choices=["kyutai-pocket", "whisper-kokoro"], required=True)
    bench.add_argument("--asr-model", required=True)
    bench.add_argument("--slm-model", default="Qwen/Qwen3-0.6B")
    bench.add_argument("--audio", type=Path, required=True)
    bench.add_argument("--output", type=Path, required=True)
    bench.add_argument("--runs", type=int, default=5)
    bench.add_argument("--reserve-mib", type=int, default=2048)
    bench.set_defaults(func=run)
    moshi = subs.add_parser("probe-moshi")
    moshi.add_argument("--binary", type=Path, required=True)
    moshi.add_argument("--model-root", type=Path, required=True)
    moshi.add_argument("--output", type=Path, required=True)
    moshi.add_argument("--context", type=int, default=250)
    moshi.add_argument("--reserve-mib", type=int, default=2048)
    moshi.add_argument("--timeout", type=int, default=180)
    moshi.set_defaults(func=probe_moshi)
    charts = subs.add_parser("export")
    charts.add_argument("--summaries", type=Path, nargs="+", required=True)
    charts.add_argument("--output", type=Path, required=True)
    charts.set_defaults(func=export)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Deterministic, virtual-time, PCM-driven barge-in replay (Python stdlib).

Frozen harness bargein_replay/1. Offline CPU-only simulation: no audio
device, voice service, API key or network. It is a model, not a live claim.

Arms (the same seeded trial inputs run through both):
  A  flush and fence the old response when the provider speech_started event
     reaches the adapter (server VAD 50 ms + uplink + event return leg).
  B  flush and fence on the adapter's local VAD (20 ms of hot PCM).
Neither arm waits for provider acknowledgement.

Timing definitions (virtual ms; t=0 is the first bot frame generated):
  latency_ms       valid trials: end of the last audible bot sample at the
                   simulated phone speaker minus the caller mic onset (>= 0).
                   p50 = statistics.median, p95 = nearest rank.
  residual_ms      valid trials: 1 ms speaker windows with audible bot audio
                   (PCM RMS >= 380) at or after the caller onset.
  false_barge_ins  negative trials (backchannel, echo, noise) that flushed.
Defaults: 4x20 ms adapter prefill, 35 ms (+0-20 ms seeded) phone hold, 12 ms
bot audio transit, 45 ms provider control apply, seeded per-trial network legs.

Frozen reference (seed 39, 200 paired trials per arm, defaults): A p50
186.46875 / p95 219 ms, 29/60 false cuts; B p50 99 / p95 121 ms, 45/60 false
cuts; --aa byte-identical; canonical output sha256 001fdb0b963c2477...

Audio attribution: fixtures/bargein/*.wav are 0.85 s excerpts of LibriSpeech
(Vassil Panayotov et al., OpenSLR SLR12 dev-clean, chapter 2277/149896),
CC BY 4.0; see fixtures/bargein/README.md. All other audio is synthesized.

Run: python3 bench/bargein_replay.py --seed 39 --trials 200 --aa --output out.json
"""

import argparse
from array import array
from collections import deque
from hashlib import sha256
from heapq import heappop, heappush
from itertools import count
import json
from math import ceil, pi, sin
from pathlib import Path
from random import Random
from statistics import median
import sys
import wave

SAMPLES_MS = 16
FRAME_MS = 20
CAPTURE_MS = 10
BOT_MS = 2600
THRESHOLD = 480  # PCM RMS, VAD; not a calibrated dB SPL threshold
AUDIBLE = 380   # PCM RMS over 1 ms at the simulated speaker
ASSETS = Path(__file__).resolve().parent / "fixtures" / "bargein"
HARNESS = "bargein_replay/1"


def pcm(ms):
    return array("h", [0]) * (ms * SAMPLES_MS)


def voice(ms, pitch, gain, period):
    """Scripted harmonic source with syllable envelope and silent gaps."""
    return array("h", (
        round(gain * max(0, sin(pi * ((i / SAMPLES_MS) % period) / period) - .34)
              * sum(sin(2 * pi * pitch * h * i / 16000) / h for h in (1, 2, 3)))
        for i in range(ms * SAMPLES_MS)
    ))


def clip(path):
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    scale = 17500 / max(map(abs, samples))
    return array("h", (round(value * scale) for value in samples))


def hot(samples, threshold):
    return sum(x * x for x in samples) >= len(samples) * threshold ** 2


class VAD:
    def __init__(self, minimum):
        self.minimum = minimum
        self.run = 0
        self.fired = False

    def observe(self, samples):
        self.run = self.run + CAPTURE_MS if hot(samples, THRESHOLD) else 0
        if not self.fired and self.run >= self.minimum:
            self.fired = True
            return True
        return False


def stimuli(n, seed):
    rng = Random(seed)
    clips = [clip(path) for path in sorted(ASSETS.glob("*.wav"))]
    if not clips:
        raise RuntimeError("Bundled LibriSpeech WAV files are missing")
    kinds = ["valid"] * 7 + ["backchannel", "echo", "noise"]
    for index in range(n):
        kind = kinds[index % len(kinds)]
        # All arms share candidate, acoustic challenge, and network timings.
        onset = rng.randint(550, 1450)
        up, ws_up, ws_back, down, phone_extra = (
            rng.randint(8, 20), rng.randint(15, 40), rng.randint(15, 45),
            rng.randint(8, 22), rng.randint(0, 20),
        )
        mic = None
        duration = None
        if kind == "valid":
            mic = rng.choice(clips)
        elif kind == "backchannel":
            mic = voice(rng.randint(35, 95), 181, 4600, 120)
        elif kind == "echo":
            duration = rng.randint(85, 200)
        else:
            mic = array("h", (rng.randint(-900, 900) for _ in range(rng.randint(8, 30) * SAMPLES_MS)))
        yield dict(id=index, kind=kind, onset=onset, mic=mic, echo_ms=duration,
                   up=up, ws_up=ws_up, ws_back=ws_back, down=down, phone_extra=phone_extra)


class FakeProvider:
    def __init__(self, cfg):
        self.vad = VAD(cfg.server_vad_ms)
        self.generating = True
        self.output_open = True
        self.history_end_ms = BOT_MS

    def control(self, command, value):
        if command == "cancel":
            self.generating = False
        elif command == "clear":
            self.output_open = False
        elif command == "truncate":
            self.history_end_ms = min(self.history_end_ms, value)
        else:
            raise ValueError(command)


def replay(trial, arm, cfg, bot):
    onset, kind = trial["onset"], trial["kind"]
    depth = cfg.depth
    hold = cfg.phone_ms + trial["phone_extra"]
    future = []
    order = count()
    trace = {}
    source_cursor = 0
    queue = deque()
    provider = FakeProvider(cfg)
    local_vad = VAD(cfg.local_vad_ms)
    muted = False
    late_dropped = 0
    speaker = pcm(BOT_MS + cfg.audio_transit_ms + depth * FRAME_MS + trial["down"] + hold)
    last_phone = None
    dropped = 0

    def post(t, priority, label, payload=None):
        heappush(future, (t, priority, next(order), label, payload))

    def flush(t, cause):
        nonlocal muted, dropped
        if muted:
            return
        muted = True  # mark old response epoch closed before clearing
        dropped = len(queue)
        queue.clear()
        trace["adapter_flush_ms"] = t
        trace["flush_cause"] = cause
        # This idealized phone commits every sent frame, so its eventual
        # item cursor (NOT wall time) is known. Live systems need feedback
        # or an estimate accounting for jitter, device queue and losses.
        trace["truncate_audio_end_ms"] = source_cursor
        for command, value in (("cancel", None), ("clear", None), ("truncate", source_cursor)):
            if command == "clear" and cfg.transport == "websocket":
                continue  # provider clear is not available on a real WebSocket
            trace[command + "_send_ms"] = t
            post(t + trial["ws_up"] + cfg.control_ms, 0, command, value)

    # Arrivals/controls precede same-timestamp output ticks. Prefill the FIFO
    # by starting scheduled output depth*20 ms after the first frame arrives.
    for index in range(BOT_MS // FRAME_MS):
        post(index * FRAME_MS, 1, "generate", index)
        post(cfg.audio_transit_ms + (index + depth) * FRAME_MS, 5, "play")
    for end in range(CAPTURE_MS, BOT_MS + 1, CAPTURE_MS):
        post(end, 3, "capture")
    trace["mic_onset_ms"] = onset
    while future:
        t, _, _, event, payload = heappop(future)
        if event == "generate":
            if provider.generating and provider.output_open:
                post(t + cfg.audio_transit_ms, 2, "audio", payload)
        elif event == "audio":
            if muted:
                late_dropped += 1
            else:
                queue.append(payload)
        elif event == "play":
            if queue:
                index = queue.popleft()
                source_cursor = (index + 1) * FRAME_MS
                trace["last_adapter_frame_ms"] = t
                last_phone = t + trial["down"], t + trial["down"] + hold
                pos = last_phone[1] * SAMPLES_MS
                first = index * FRAME_MS * SAMPLES_MS
                size = FRAME_MS * SAMPLES_MS
                speaker[pos:pos + size] = bot[first:first + size]
        elif event == "capture":
            start = (t - CAPTURE_MS) * SAMPLES_MS
            packet = pcm(CAPTURE_MS)
            origin = onset * SAMPLES_MS
            mic = trial["mic"]
            if mic is not None:
                for j in range(len(packet)):
                    offset = start + j - origin
                    if 0 <= offset < len(mic):
                        packet[j] = mic[offset]
            elif kind == "echo":
                for j in range(len(packet)):
                    offset = start + j - origin
                    ref = start + j - 35 * SAMPLES_MS
                    if 0 <= offset < trial["echo_ms"] * SAMPLES_MS and ref >= 0:
                        packet[j] = round(cfg.echo_gain * speaker[ref])
            if t - CAPTURE_MS <= onset < t:
                trace["onset_capture_frame_end_ms"] = t
                trace["phone_uplink_send_ms"] = t
                trace["adapter_onset_frame_receive_ms"] = t + trial["up"]
                trace["adapter_onset_frame_ws_send_ms"] = t + trial["up"]
                trace["provider_onset_frame_receive_ms"] = t + trial["up"] + trial["ws_up"]
            post(t + trial["up"], 3, "uplink", packet)
        elif event == "uplink":
            if local_vad.observe(payload):
                trace["local_vad_ms"] = t
                if arm == "B":
                    flush(t, "local_vad")
            # PCM keeps flowing upstream even if bot audio has been muted.
            post(t + trial["ws_up"], 3, "server_pcm", payload)
        elif event == "server_pcm":
            if provider.vad.observe(payload):
                trace["provider_speech_started_emit_ms"] = t
                post(t + trial["ws_back"], 3, "speech_started")
        elif event == "speech_started":
            trace["provider_speech_started_rx_ms"] = t
            if arm == "A":
                flush(t, "provider_vad")
        elif event in ("cancel", "clear", "truncate"):
            provider.control(event, payload)
            trace[event + "_apply_ms"] = t
            post(t + trial["ws_back"], 3, "ack", event)
        elif event == "ack":
            trace[payload + "_ack_rx_ms"] = t
        else:
            raise ValueError(event)

    if last_phone:
        trace["phone_last_frame_jitter_enqueue_ms"], trace["phone_last_frame_render_start_ms"] = last_phone
    audible = [ms for ms in range(len(speaker) // SAMPLES_MS)
               if hot(speaker[ms * SAMPLES_MS:(ms + 1) * SAMPLES_MS], AUDIBLE)]
    end = None
    if audible:
        start = audible[-1] * SAMPLES_MS
        last = max(i for i in range(start, start + SAMPLES_MS) if abs(speaker[i]) >= AUDIBLE)
        end = (last + 1) / SAMPLES_MS  # end boundary of the last audible sample
    trace["phone_last_audible_end_ms"] = end
    residual = sum(ms >= onset for ms in audible)
    latency = max(0, end - onset) if end is not None else 0
    trace["provider_history_end_ms"] = provider.history_end_ms
    return dict(trial=trial["id"], kind=kind, latency_ms=latency,
                residual_ms=residual, false_barge_in=kind != "valid" and muted,
                missed_valid=kind == "valid" and not muted,
                queue_dropped_frames=dropped, late_dropped_frames=late_dropped,
                timing={k: trial[k] for k in ("up", "ws_up", "ws_back", "down", "phone_extra")},
                trace=trace)


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"p50": None, "p95": None}
    return {"p50": median(values), "p95": values[ceil(.95 * len(values)) - 1]}


def run(n, seed, cfg, bot):
    cases = list(stimuli(n, seed))
    result = dict(seed=seed, trials_per_arm=n, parameters={
        k: v for k, v in vars(cfg).items() if k not in ("aa", "output", "seed", "trials")
    }, arms={})
    for arm in ("A", "B"):
        rows = [replay(case, arm, cfg, bot) for case in cases]
        valid = [row for row in rows if row["kind"] == "valid"]
        result["arms"][arm] = dict(
            valid_n=len(valid),
            missed_valid=sum(row["missed_valid"] for row in valid),
            valid_over_150_or_missed=sum(row["missed_valid"] or row["latency_ms"] > 150 for row in valid),
            latency_ms=percentiles([row["latency_ms"] for row in valid]),
            residual_ms=percentiles([row["residual_ms"] for row in valid]),
            false_barge_ins=sum(row["false_barge_in"] for row in rows),
            negative_n=n - len(valid),
            negative_by_kind={kind: {
                "n": sum(row["kind"] == kind for row in rows),
                "false": sum(row["kind"] == kind and row["false_barge_in"] for row in rows),
            } for kind in ("backchannel", "echo", "noise")},
            trials=rows,
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=200, help="paired trials per arm")
    parser.add_argument("--seed", type=int, default=39)
    parser.add_argument("--depth", type=int, default=4, help="adapter prefill in 20 ms frames")
    parser.add_argument("--local-vad-ms", type=int, default=20)
    parser.add_argument("--server-vad-ms", type=int, default=50)
    parser.add_argument("--control-ms", type=int, default=45, help="provider apply time after command arrival")
    parser.add_argument("--audio-transit-ms", type=int, default=12, help="provider-to-adapter bot audio transit")
    parser.add_argument("--phone-ms", type=int, default=35, help="base phone jitter+device delay")
    parser.add_argument("--echo-gain", type=float, default=.65)
    parser.add_argument("--transport", choices=("websocket", "provider-buffer"), default="websocket",
                        help="provider-buffer also exercises fake output_audio_buffer.clear")
    parser.add_argument("--aa", action="store_true", help="repeat and compare canonical JSON bytes")
    parser.add_argument("--output", type=Path, help="write full canonical per-trial JSON")
    cfg = parser.parse_args()
    if cfg.trials < 10 or not 0 <= cfg.depth <= 50 or not 0 <= cfg.echo_gain <= 1:
        parser.error("require trials >= 10, depth in [0, 50], echo gain in [0, 1]")
    if any(not 0 <= value <= 1000 for value in (cfg.control_ms, cfg.audio_transit_ms, cfg.phone_ms)):
        parser.error("control, transit and phone delays must be 0..1000 ms")
    if any(value <= 0 or value % CAPTURE_MS for value in (cfg.local_vad_ms, cfg.server_vad_ms)):
        parser.error("VAD durations must be positive multiples of 10 ms")

    bot = voice(BOT_MS, 142, 11000, 115)
    result = run(cfg.trials, cfg.seed, cfg, bot)

    def canonical(value):
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

    data = canonical(result)
    if cfg.aa and canonical(run(cfg.trials, cfg.seed, cfg, bot)) != data:
        raise SystemExit("A/A mismatch")
    if cfg.output:
        cfg.output.write_bytes(data)
    summary = {**result, "sha256": sha256(data).hexdigest(), "aa_identical": True if cfg.aa else None}
    summary["arms"] = {arm: {k: v for k, v in values.items() if k != "trials"}
                       for arm, values in result["arms"].items()}
    print(json.dumps(summary, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

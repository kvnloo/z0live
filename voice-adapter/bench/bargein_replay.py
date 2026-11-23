#!/usr/bin/env python3
"""Deterministic, virtual-time, PCM-driven barge-in replay (Python stdlib).

Frozen harness bargein_replay/1. Offline CPU-only simulation: no audio
device, voice service, API key or network. It is a model, not a live claim.

Arms (the same seeded trial inputs run through both):
  A  flush and fence the old response when the provider speech_started event
     reaches the adapter (server VAD 50 ms + uplink + event return leg).
  B  flush and fence on the adapter's local VAD (20 ms of hot PCM).
  C  (opt-in, --arm-c) B's local uplink path with a FlushGate that sees only
     uplink PCM and the render log of bot frames the adapter itself sent:
     an echo gate (normalized render cross-correlation over a 0..250 ms lag
     search) plus a minimum-speech gate. Without --arm-c the output bytes
     are unchanged. CAVEAT: the sim echo is an exact delayed and scaled copy
     of the render, so a correlation gate finds it far more easily than real
     AEC residuals (nonlinear, filtered, time-varying, mixed with double-talk).
     A C result in this sim is an upper bound, not live evidence.
No arm waits for provider acknowledgement.

Echo models (--echo-model):
  copy      default, bargein_replay/1 bytes: echo = 0.65 x speaker, 35 ms late.
  residual  opt-in, output marked bargein_replay/2. Per trial, from a second
            RNG (the base trial stream is untouched): bulk delay 20-120 ms,
            sparse room impulse response (direct tap + 12 exponentially
            decaying reflections over 10-60 ms), ERL 6-20 dB, tanh loudspeaker
            soft clip, Gaussian near-end noise (RMS 40-160) on every capture
            sample, and double-talk: about half of the valid trials get
            85-200 ms of residual echo under the caller's speech from onset.
            Not modelled: time-varying echo paths, AEC adaptation, drift.

Bot render (--bot-render):
  periodic  default, unchanged bytes: synthetic harmonic voice (142 Hz, 115 ms
            syllables). Its self-similarity flatters the arm C echo gate.
  recorded  opt-in, output marked bargein_replay/3. Per trial, from its own
            RNG, the bot render is a seeded rotation of one LibriSpeech fixture
            clip tiled to the bot length. In valid trials that clip is never the
            caller's onset clip (asserted), so bot and caller audio are
            disjoint. Speech pauses can end audible bot audio earlier than the
            dense periodic render does. Adds echo-rho, veto and double-talk
            summaries.

Arm D (--arm-d, opt-in, output marked bargein_replay/4; arms A/B/C unchanged):
  B's local uplink path with an EnergyGate (Geigel-style energy-ratio echo
  gate): a hot packet is vetoed when near RMS <= 10^(k/20) x the max render
  RMS over the 0..energy_lag_max_ms lag window of the adapter's own render
  log (--energy-k-db k); flush after --energy-min-speech-ms of consecutive
  unvetoed hot packets. Same adapter-only inputs as arm C, no spectral match.

Arm E (--arm-e, opt-in, output marked bargein_replay/6; arms A-D unchanged):
  arm D's energy-ratio gate with an online per-call echo-gain estimate (EWMA in dB of near/render over
  render-active, non-speech packets; --erl-estimator quiet|vetoed, --erl-alpha, --erl-prior-db); a hot
  packet is vetoed when near/render dB <= estimate + --erl-margin-db; flush after --erl-min-speech-ms.
  CAVEAT: the residual echo model only puts echo in the uplink inside a trial's echo window (from onset),
  so bot-only windows carry noise, not echo: in this sim the estimate tracks noise/render, not the ERL.

Stress flags (opt-in, output marked bargein_replay/5; without them nothing changes):
  --caller-level-db-range LO,HI  per valid trial, caller gain g ~ U(LO, HI) dB
            relative to the fixture level (bot render stays at its level), drawn
            from its own RNG; the caller clip is scaled by 10^(g/20). Negative
            trials are unchanged.
  --erl-db-range LO,HI  (with --echo-model residual) residual ERL ~ U(LO, HI)
            dB instead of U(6, 20); only the bounds of that one draw change.

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
from math import ceil, exp, log10, pi, sin, sqrt, tanh
from operator import mul
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
HARNESS_RESIDUAL = "bargein_replay/2"  # only with --echo-model residual
HARNESS_RECORDED = "bargein_replay/3"  # only with --bot-render recorded
HARNESS_ENERGY = "bargein_replay/4"  # only with --arm-d
HARNESS_STRESS = "bargein_replay/5"  # only with --caller-level-db-range / --erl-db-range
HARNESS_ERL = "bargein_replay/6"  # only with --arm-e
ERL_DB_RANGE = (6, 20)
REFLECTIONS = 12


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


class FlushGate:
    """Arm C flush decision from adapter-side signals only.

    Inputs: uplink PCM packets at adapter receive time (observe) and the bot
    PCM frames this adapter sent downlink at its send times (render). Nothing
    else: no labels, no echo-model parameters, no network legs, no speaker.

    (a) Echo gate: a hot packet whose last gate_window_ms of near-end audio has
        normalized cross-correlation >= gate_echo_rho with the render log at
        some lag in 0..gate_lag_max_ms is explained by render, not speech.
        gate_echo_rho <= 0 disables it.
    (b) Minimum speech: flush after gate_min_speech_ms of consecutive hot,
        unexplained packets.
    """

    def __init__(self, cfg, span_ms):
        self.rho = cfg.gate_echo_rho
        self.minimum = cfg.gate_min_speech_ms
        self.window = cfg.gate_window_ms * SAMPLES_MS
        self.lag_max = cfg.gate_lag_max_ms * SAMPLES_MS
        self.sent = pcm(span_ms)
        self.near = pcm(span_ms)
        self.run = 0
        self.fired = False
        self.vetoes = 0
        self.max_rho = 0.0

    def render(self, t, frame):
        pos = t * SAMPLES_MS
        self.sent[pos:pos + len(frame)] = frame

    def correlation(self, end):
        start = end - self.window
        if start < 0:
            return 0.0
        x = self.near[start:end]
        ex = sum(v * v for v in x)
        lo = max(0, start - self.lag_max)
        seg = self.sent[lo:end]
        prefix = [0]
        for v in seg:
            prefix.append(prefix[-1] + v * v)
        if not ex or not prefix[-1]:
            return 0.0
        best = 0.0
        n = self.window
        for offset in range(start - lo, -1, -1):  # lag 0 .. lag_max
            er = prefix[offset + n] - prefix[offset]
            if er:
                num = sum(map(mul, x, seg[offset:offset + n]))
                if num > 0:
                    best = max(best, num / sqrt(ex * er))
        return best

    def observe(self, t, packet):
        end = t * SAMPLES_MS
        self.near[end - len(packet):end] = packet
        speech = hot(packet, THRESHOLD)
        if speech and self.rho > 0:
            rho = self.correlation(end)
            self.max_rho = max(self.max_rho, rho)
            if rho >= self.rho:
                speech = False
                self.vetoes += 1
        self.run = self.run + CAPTURE_MS if speech else 0
        if not self.fired and self.run >= self.minimum:
            self.fired = True
            return True
        return False


class EnergyGate:
    """Arm D flush decision (bargein_replay/4): Geigel-style energy-ratio echo gate.

    Same inputs as FlushGate: uplink PCM packets at adapter receive time
    (observe) and the bot PCM frames this adapter sent (render). Nothing else.

    (a) Echo gate: a hot packet whose energy is explained by the predicted
        echo energy is vetoed: near RMS <= 10^(energy_k_db / 20) x the max RMS
        of any packet-length render window that ended 0..energy_lag_max_ms
        before the packet ended. Compared in squared energy (no log), so the
        decision is exact float arithmetic.
    (b) Minimum speech: flush after energy_min_speech_ms of consecutive hot,
        unexplained packets.
    """

    def __init__(self, cfg, span_ms):
        self.ratio2 = 10 ** (cfg.energy_k_db / 10)  # (amplitude ratio)^2
        self.minimum = cfg.energy_min_speech_ms
        self.lag_max = cfg.energy_lag_max_ms * SAMPLES_MS
        self.sent = pcm(span_ms)
        self.run = 0
        self.fired = False
        self.vetoes = 0
        self.max_ratio2 = None  # max near/render energy ratio over hot packets (reporting only)

    def render(self, t, frame):
        pos = t * SAMPLES_MS
        self.sent[pos:pos + len(frame)] = frame

    def reference(self, end, n):
        """Max sum of squares over render windows of n samples ending in [end - lag_max, end]."""
        lo = max(0, end - self.lag_max - n)
        seg = self.sent[lo:end]
        prefix = [0]
        for v in seg:
            prefix.append(prefix[-1] + v * v)
        return max((prefix[i + n] - prefix[i] for i in range(0, len(seg) - n + 1)), default=0)

    def observe(self, t, packet):
        end = t * SAMPLES_MS
        speech = hot(packet, THRESHOLD)
        if speech:
            n = len(packet)
            near = sum(x * x for x in packet)
            ref = self.reference(end, n)
            if ref:
                ratio2 = near / ref
                self.max_ratio2 = ratio2 if self.max_ratio2 is None else max(self.max_ratio2, ratio2)
                if near <= self.ratio2 * ref:
                    speech = False
                    self.vetoes += 1
        self.run = self.run + CAPTURE_MS if speech else 0
        if not self.fired and self.run >= self.minimum:
            self.fired = True
            return True
        return False


class ErlGate:
    """Arm E flush decision (bargein_replay/6): energy-ratio echo gate with an online echo-gain estimate.

    Same inputs as EnergyGate: uplink PCM packets at adapter receive time (observe) and the bot PCM frames
    this adapter sent (render). Nothing else: the sim's ERL, echo path, labels and onset are never read.

    Reference: exactly EnergyGate.reference (max sum of squares over packet-length render windows ending
    0..energy_lag_max_ms before the packet end), kept incrementally with a monotonic deque. Render samples
    before the packet end are final (frames are written at their send time, never earlier).
    Estimate est_db (starts at erl_prior_db) of the near/render ratio in dB, EWMA with erl_alpha, updated
    from render-active packets (reference RMS >= VAD threshold) that are not counted as speech, while the
    gate has not fired: estimator "quiet" uses only packets whose uplink is below the VAD threshold (bot-only
    windows); "vetoed" also uses hot packets this gate vetoed.
    (a) Echo gate: a hot packet is vetoed when near <= 10^((est_db + erl_margin_db) / 10) x reference.
    (b) Minimum speech: flush after erl_min_speech_ms of consecutive hot, unexplained packets.
    With erl_alpha 0, prior k and margin 0 the decisions equal EnergyGate with energy_k_db k.
    """

    def __init__(self, cfg, span_ms):
        self.estimator = cfg.erl_estimator
        self.alpha = cfg.erl_alpha
        self.est_db = cfg.erl_prior_db
        self.margin_db = cfg.erl_margin_db
        self.minimum = cfg.erl_min_speech_ms
        self.lag_max = cfg.energy_lag_max_ms * SAMPLES_MS
        self.sent = pcm(span_ms)
        self.prefix = [0]  # prefix[i] = sum of squares of sent[:i]
        self.windows = deque()  # (window end, energy), energies strictly decreasing
        self.n = None
        self.run = 0
        self.fired = False
        self.vetoes = 0
        self.updates = 0
        self.max_ratio2 = None  # max near/render energy ratio over hot packets (reporting only)

    def render(self, t, frame):
        pos = t * SAMPLES_MS
        self.sent[pos:pos + len(frame)] = frame

    def reference(self, end, n):
        """Same value as EnergyGate.reference(end, n); ends must not decrease, n is fixed."""
        if self.n is None:
            self.n = n
        assert n == self.n
        sent, prefix, windows = self.sent, self.prefix, self.windows
        while len(prefix) <= end:
            v = sent[len(prefix) - 1]
            prefix.append(prefix[-1] + v * v)
            p = len(prefix) - 1
            if p >= n:
                w = prefix[p] - prefix[p - n]
                while windows and windows[-1][1] <= w:
                    windows.pop()
                windows.append((p, w))
        first = max(n, end - self.lag_max)
        while windows and windows[0][0] < first:
            windows.popleft()
        return windows[0][1] if windows else 0

    def observe(self, t, packet):
        end = t * SAMPLES_MS
        n = len(packet)
        ref = self.reference(end, n)
        near = sum(x * x for x in packet)
        loud = hot(packet, THRESHOLD)
        speech = loud
        vetoed = False
        if speech and ref:
            ratio2 = near / ref
            self.max_ratio2 = ratio2 if self.max_ratio2 is None else max(self.max_ratio2, ratio2)
            if near <= 10 ** ((self.est_db + self.margin_db) / 10) * ref:
                speech = False
                vetoed = True
                self.vetoes += 1
        if (not self.fired and near and ref >= n * THRESHOLD ** 2
                and (not loud or (vetoed and self.estimator == "vetoed"))):
            self.est_db = (1 - self.alpha) * self.est_db + self.alpha * 10 * log10(near / ref)
            self.updates += 1
        self.run = self.run + CAPTURE_MS if speech else 0
        if not self.fired and self.run >= self.minimum:
            self.fired = True
            return True
        return False


def residual_echo(seed, index, kind, erl_db_range=None):
    """bargein_replay/2 per-trial echo path, drawn from its own RNG (erl_db_range: bargein_replay/5)."""
    rng = Random(f"{HARNESS_RESIDUAL}:{seed}:{index}")
    delay_ms = rng.randint(20, 120)
    rir_ms = rng.randint(10, 60)
    span = rir_ms * SAMPLES_MS
    taps = [(0, 1.0)]
    for _ in range(REFLECTIONS):
        d = rng.randint(SAMPLES_MS, span)
        sign = rng.choice((-1, 1))
        taps.append((d, sign * rng.uniform(.2, .6) * exp(-3 * d / span)))
    erl_db = rng.uniform(*(erl_db_range or ERL_DB_RANGE))  # one random() draw whatever the bounds
    clip_level = rng.uniform(12000, 20000)
    noise_rms = rng.uniform(40, 160)
    noise_rng = Random(rng.getrandbits(32))
    noise = array("h", (round(noise_rng.gauss(0, noise_rms)) for _ in range(BOT_MS * SAMPLES_MS)))
    double_talk = kind == "valid" and rng.random() < .5
    double_talk_ms = rng.randint(85, 200) if double_talk else None
    return dict(delay_ms=delay_ms, rir_ms=rir_ms, taps=taps, erl_db=erl_db, gain=10 ** (-erl_db / 20),
                clip_level=clip_level, noise_rms=noise_rms, noise=noise,
                double_talk=double_talk, double_talk_ms=double_talk_ms)


def recorded_bot(seed, index, clips, names, exclude):
    """bargein_replay/3 bot render: a seeded rotation of a fixture clip that is not `exclude`."""
    candidates = [k for k, name in enumerate(names) if name != exclude]
    assert candidates, "no recorded bot clip disjoint from the caller clip"
    rng = Random(f"{HARNESS_RECORDED}:bot:{seed}:{index}")
    k = rng.choice(candidates)
    source = clips[k]
    offset = rng.randrange(len(source))
    bot = array("h", (source[(offset + i) % len(source)] for i in range(BOT_MS * SAMPLES_MS)))
    assert names[k] != exclude
    return names[k], offset, bot


def caller_gain_db(seed, index, level_range):
    """bargein_replay/5 per-trial caller gain in dB, from its own RNG (other streams untouched)."""
    return Random(f"{HARNESS_STRESS}:caller:{seed}:{index}").uniform(*level_range)


def scaled(samples, gain_db):
    scale = 10 ** (gain_db / 20)
    return array("h", (max(-32768, min(32767, round(v * scale))) for v in samples))


def stimuli(n, seed, echo_model="copy", bot_render="periodic", caller_level_db_range=None, erl_db_range=None):
    rng = Random(seed)
    paths = sorted(ASSETS.glob("*.wav"))
    names = [path.name for path in paths]
    clips = [clip(path) for path in paths]
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
        trial = dict(id=index, kind=kind, onset=onset, mic=mic, echo_ms=duration,
                     up=up, ws_up=ws_up, ws_back=ws_back, down=down, phone_extra=phone_extra)
        if echo_model == "residual":
            trial["residual"] = residual_echo(seed, index, kind, erl_db_range)
        if bot_render == "recorded":
            mic_clip = next((names[k] for k, c in enumerate(clips) if c is mic), None)
            assert (mic_clip is not None) == (kind == "valid")
            trial["mic_clip"] = mic_clip
            trial["bot_clip"], trial["bot_offset"], trial["bot"] = recorded_bot(seed, index, clips, names, mic_clip)
            assert trial["bot_clip"] != mic_clip  # bot render never reuses the caller's onset clip
        if caller_level_db_range and kind == "valid":
            trial["caller_gain_db"] = caller_gain_db(seed, index, caller_level_db_range)
            trial["mic"] = scaled(mic, trial["caller_gain_db"])
        yield trial


def capture(trial, speaker, start, size, cfg):
    """Near-end mic samples [start, start + size) as the phone uplinks them."""
    packet = array("h", [0]) * size
    origin = trial["onset"] * SAMPLES_MS
    mic = trial["mic"]
    if getattr(cfg, "echo_model", "copy") != "residual":
        if mic is not None:
            for j in range(size):
                offset = start + j - origin
                if 0 <= offset < len(mic):
                    packet[j] = mic[offset]
        elif trial["kind"] == "echo":
            for j in range(size):
                offset = start + j - origin
                ref = start + j - 35 * SAMPLES_MS
                if 0 <= offset < trial["echo_ms"] * SAMPLES_MS and ref >= 0:
                    packet[j] = round(cfg.echo_gain * speaker[ref])
        return packet
    r = trial["residual"]
    echo_ms = trial["echo_ms"] if trial["kind"] == "echo" else r["double_talk_ms"]
    window = (echo_ms or 0) * SAMPLES_MS
    bulk = r["delay_ms"] * SAMPLES_MS
    level, gain, noise = r["clip_level"], r["gain"], r["noise"]
    for j in range(size):
        i = start + j
        offset = i - origin
        value = mic[offset] if mic is not None and 0 <= offset < len(mic) else 0
        if 0 <= offset < window:
            echo = 0.0
            for d, h in r["taps"]:
                ref = i - bulk - d
                if 0 <= ref < len(speaker):
                    echo += h * level * tanh(speaker[ref] / level)
            value += gain * echo
        if i < len(noise):
            value += noise[i]
        packet[j] = max(-32768, min(32767, round(value)))
    return packet


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
    bot = trial.get("bot", bot)  # bargein_replay/3 per-trial recorded render
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
    gate = FlushGate(cfg, len(speaker) // SAMPLES_MS) if arm == "C" else None
    egate = EnergyGate(cfg, len(speaker) // SAMPLES_MS) if arm == "D" else None
    lgate = ErlGate(cfg, len(speaker) // SAMPLES_MS) if arm == "E" else None

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
                if gate:
                    gate.render(t, bot[first:first + size])  # what the adapter sent, at its send time
                if egate:
                    egate.render(t, bot[first:first + size])
                if lgate:
                    lgate.render(t, bot[first:first + size])
        elif event == "capture":
            packet = capture(trial, speaker, (t - CAPTURE_MS) * SAMPLES_MS, CAPTURE_MS * SAMPLES_MS, cfg)
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
            if gate and gate.observe(t, payload):
                trace["gate_fire_ms"] = t
                flush(t, "gated_local_vad")
            if egate and egate.observe(t, payload):
                trace["energy_fire_ms"] = t
                flush(t, "energy_gated_local_vad")
            if lgate and lgate.observe(t, payload):
                trace["erl_fire_ms"] = t
                flush(t, "erl_gated_local_vad")
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
    if gate:
        trace["gate_echo_vetoes"] = gate.vetoes
        trace["gate_max_rho"] = round(gate.max_rho, 4)
    if egate:
        trace["energy_vetoes"] = egate.vetoes
        trace["energy_max_ratio_db"] = (None if egate.max_ratio2 is None
                                        else round(10 * log10(egate.max_ratio2), 2))
    if lgate:
        trace["erl_vetoes"] = lgate.vetoes
        trace["erl_updates"] = lgate.updates
        trace["erl_final_est_db"] = round(lgate.est_db, 2)
        trace["energy_max_ratio_db"] = (None if lgate.max_ratio2 is None
                                        else round(10 * log10(lgate.max_ratio2), 2))
    row = dict(trial=trial["id"], kind=kind, latency_ms=latency,
               residual_ms=residual, false_barge_in=kind != "valid" and muted,
               missed_valid=kind == "valid" and not muted,
               queue_dropped_frames=dropped, late_dropped_frames=late_dropped,
               timing={k: trial[k] for k in ("up", "ws_up", "ws_back", "down", "phone_extra")},
               trace=trace)
    if "residual" in trial and getattr(cfg, "echo_model", "copy") == "residual":
        r = trial["residual"]
        row["echo_path"] = dict(delay_ms=r["delay_ms"], rir_ms=r["rir_ms"], erl_db=round(r["erl_db"], 3),
                                double_talk=r["double_talk"], double_talk_ms=r["double_talk_ms"])
    if "caller_gain_db" in trial:
        row["caller_gain_db"] = round(trial["caller_gain_db"], 3)
    if "bot" in trial:
        row["bot_render"] = dict(clip=trial["bot_clip"], offset=trial["bot_offset"], mic_clip=trial["mic_clip"])
    return row


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"p50": None, "p95": None}
    return {"p50": median(values), "p95": values[ceil(.95 * len(values)) - 1]}


def quantiles(values):
    """Nearest-rank p10 / p90 and statistics.median p50 (bargein_replay/3 summaries)."""
    values = sorted(values)
    if not values:
        return {"n": 0, "p10": None, "p50": None, "p90": None}
    rank = lambda q: values[max(0, ceil(q * len(values)) - 1)]
    return {"n": len(values), "p10": rank(.1), "p50": median(values), "p90": rank(.9)}


def run(n, seed, cfg, bot):
    echo_model = getattr(cfg, "echo_model", "copy")
    bot_render = getattr(cfg, "bot_render", "periodic")
    recorded = bot_render == "recorded"
    stress = {k: getattr(cfg, k, None) for k in ("caller_level_db_range", "erl_db_range")}
    stress = {k: tuple(v) for k, v in stress.items() if v is not None}
    if stress:
        cases = list(stimuli(n, seed, echo_model, bot_render, **stress))
    else:
        cases = list(stimuli(n, seed, echo_model, bot_render)) if recorded else list(stimuli(n, seed, echo_model))
    arm_c = getattr(cfg, "arm_c", False)
    arm_d = getattr(cfg, "arm_d", False)
    arm_e = getattr(cfg, "arm_e", False)
    result = dict(seed=seed, trials_per_arm=n, parameters={
        k: v for k, v in vars(cfg).items() if k not in ("aa", "output", "seed", "trials", "arm_c", "arm_d", "arm_e")
        and (arm_c or not k.startswith("gate_"))  # default run keeps the frozen bytes
        and (arm_d or arm_e or not k.startswith("energy_"))
        and (arm_e or not k.startswith("erl_") or k == "erl_db_range")  # erl_db_range: bargein_replay/5 stress
        and (k != "echo_model" or echo_model != "copy")
        and (k != "bot_render" or recorded)
        and (k not in ("caller_level_db_range", "erl_db_range") or v is not None)
    }, arms={})
    if echo_model != "copy":
        result["harness"] = HARNESS_RESIDUAL
    if recorded:
        result["harness"] = HARNESS_RECORDED
    if arm_d:
        result["harness"] = HARNESS_ENERGY
    if stress:
        result["harness"] = HARNESS_STRESS
    if arm_e:
        result["harness"] = HARNESS_ERL
    arms = ("A", "B", "C") if arm_c else ("A", "B")
    for arm in arms + (("D",) if arm_d else ()) + (("E",) if arm_e else ()):
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
        if recorded:
            summary = result["arms"][arm]
            summary["double_talk_latency_ms"] = percentiles(
                [row["latency_ms"] for row in valid if row.get("echo_path", {}).get("double_talk")])
            if arm == "C":
                summary["valid_trials_with_veto"] = sum(row["trace"]["gate_echo_vetoes"] > 0 for row in valid)
                summary["echo_trial_max_rho"] = quantiles(
                    [row["trace"]["gate_max_rho"] for row in rows if row["kind"] == "echo"])
                summary["valid_trial_max_rho"] = quantiles([row["trace"]["gate_max_rho"] for row in valid])
        if arm == "D":
            summary = result["arms"][arm]
            if not recorded:
                summary["double_talk_latency_ms"] = percentiles(
                    [row["latency_ms"] for row in valid if row.get("echo_path", {}).get("double_talk")])
            summary["valid_trials_with_veto"] = sum(row["trace"]["energy_vetoes"] > 0 for row in valid)

            def ratios(subset):
                return quantiles([row["trace"]["energy_max_ratio_db"] for row in subset
                                  if row["trace"]["energy_max_ratio_db"] is not None])
            summary["echo_trial_max_ratio_db"] = ratios([row for row in rows if row["kind"] == "echo"])
            summary["valid_trial_max_ratio_db"] = ratios(valid)
        if arm == "E":
            summary = result["arms"][arm]
            summary["double_talk_latency_ms"] = percentiles(
                [row["latency_ms"] for row in valid if row.get("echo_path", {}).get("double_talk")])
            summary["valid_trials_with_veto"] = sum(row["trace"]["erl_vetoes"] > 0 for row in valid)
            summary["final_est_db"] = quantiles([row["trace"]["erl_final_est_db"] for row in rows])
            summary["echo_trial_max_ratio_db"] = quantiles([row["trace"]["energy_max_ratio_db"] for row in rows
                                                            if row["kind"] == "echo"
                                                            and row["trace"]["energy_max_ratio_db"] is not None])
    return result


def db_range(text):
    try:
        lo, hi = (float(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected LO,HI in dB, e.g. -10,0")
    if lo > hi:
        raise argparse.ArgumentTypeError("LO must be <= HI")
    return [lo, hi]


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
    parser.add_argument("--arm-c", action="store_true",
                        help="add arm C: local VAD timing gated by a render echo gate and minimum speech")
    parser.add_argument("--gate-echo-rho", type=float, default=.9,
                        help="arm C: veto a hot packet whose render correlation reaches this (<=0 disables)")
    parser.add_argument("--gate-min-speech-ms", type=int, default=40,
                        help="arm C: consecutive unexplained hot speech before flush")
    parser.add_argument("--gate-window-ms", type=int, default=20, help="arm C: near-end correlation window")
    parser.add_argument("--gate-lag-max-ms", type=int, default=250, help="arm C: render lag search 0..N ms")
    parser.add_argument("--arm-d", action="store_true",
                        help="add arm D (bargein_replay/4): local VAD timing gated by an energy-ratio echo gate")
    parser.add_argument("--energy-k-db", type=float, default=-6.0,
                        help="arm D: veto a hot packet when near RMS <= 10^(k/20) x max render RMS in the lag window")
    parser.add_argument("--energy-min-speech-ms", type=int, default=40,
                        help="arm D: consecutive unvetoed hot speech before flush")
    parser.add_argument("--energy-lag-max-ms", type=int, default=250, help="arm D: render lag window 0..N ms")
    parser.add_argument("--arm-e", action="store_true",
                        help="add arm E (bargein_replay/6): energy-ratio gate with an online echo-gain estimate")
    parser.add_argument("--erl-estimator", choices=("quiet", "vetoed"), default="quiet",
                        help="arm E: update from bot-only packets (quiet) or also from vetoed hot packets")
    parser.add_argument("--erl-alpha", type=float, default=.1, help="arm E: EWMA weight of each update (0 = frozen)")
    parser.add_argument("--erl-prior-db", type=float, default=-12.0, help="arm E: initial near/render estimate dB")
    parser.add_argument("--erl-margin-db", type=float, default=6.0,
                        help="arm E: veto a hot packet when near/render dB <= estimate + margin")
    parser.add_argument("--erl-min-speech-ms", type=int, default=30,
                        help="arm E: consecutive unvetoed hot speech before flush")
    parser.add_argument("--echo-model", choices=("copy", "residual"), default="copy",
                        help="residual: bargein_replay/2 filtered, attenuated, clipped, noisy echo plus double-talk")
    parser.add_argument("--bot-render", choices=("periodic", "recorded"), default="periodic",
                        help="recorded: bargein_replay/3 LibriSpeech bot render disjoint from the caller clip")
    parser.add_argument("--caller-level-db-range", type=db_range, default=None, metavar="LO,HI",
                        help="bargein_replay/5: per valid trial caller gain U(LO, HI) dB, own RNG stream")
    parser.add_argument("--erl-db-range", type=db_range, default=None, metavar="LO,HI",
                        help="bargein_replay/5 (needs --echo-model residual): residual ERL U(LO, HI) dB, default 6,20")
    parser.add_argument("--aa", action="store_true", help="repeat and compare canonical JSON bytes")
    parser.add_argument("--output", type=Path, help="write full canonical per-trial JSON")
    cfg = parser.parse_args()
    if cfg.trials < 10 or not 0 <= cfg.depth <= 50 or not 0 <= cfg.echo_gain <= 1:
        parser.error("require trials >= 10, depth in [0, 50], echo gain in [0, 1]")
    if any(not 0 <= value <= 1000 for value in (cfg.control_ms, cfg.audio_transit_ms, cfg.phone_ms)):
        parser.error("control, transit and phone delays must be 0..1000 ms")
    if any(value <= 0 or value % CAPTURE_MS for value in (cfg.local_vad_ms, cfg.server_vad_ms)):
        parser.error("VAD durations must be positive multiples of 10 ms")
    if cfg.gate_min_speech_ms <= 0 or cfg.gate_min_speech_ms % CAPTURE_MS or cfg.gate_echo_rho > 1:
        parser.error("gate minimum speech must be a positive multiple of 10 ms; gate rho <= 1")
    if not CAPTURE_MS <= cfg.gate_window_ms <= 100 or not 0 <= cfg.gate_lag_max_ms <= 1000:
        parser.error("gate window must be 10..100 ms and lag max 0..1000 ms")
    if cfg.energy_min_speech_ms <= 0 or cfg.energy_min_speech_ms % CAPTURE_MS or not -200 <= cfg.energy_k_db <= 40:
        parser.error("energy minimum speech must be a positive multiple of 10 ms; k in [-200, 40] dB")
    if not 0 <= cfg.energy_lag_max_ms <= 1000:
        parser.error("energy lag max must be 0..1000 ms")
    if (cfg.erl_min_speech_ms <= 0 or cfg.erl_min_speech_ms % CAPTURE_MS or not 0 <= cfg.erl_alpha <= 1
            or not -60 <= cfg.erl_margin_db <= 60 or not -100 <= cfg.erl_prior_db <= 40):
        parser.error("erl minimum speech must be a positive multiple of 10 ms; alpha in [0, 1]; "
                     "margin in [-60, 60] dB; prior in [-100, 40] dB")
    if cfg.caller_level_db_range and not -60 <= cfg.caller_level_db_range[0] <= cfg.caller_level_db_range[1] <= 20:
        parser.error("caller level range must be LO,HI with -60 <= LO <= HI <= 20 dB")
    if cfg.erl_db_range and (cfg.echo_model != "residual" or not 0 <= cfg.erl_db_range[0] <= cfg.erl_db_range[1] <= 60):
        parser.error("ERL range needs --echo-model residual and 0 <= LO <= HI <= 60 dB")

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

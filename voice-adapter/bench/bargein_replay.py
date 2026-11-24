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

Continuous echo (--echo-continuous, opt-in, needs --echo-model residual, output marked bargein_replay/7):
  the same residual echo path (delay, RIR, ERL, soft clip; same per-trial draws) is added to every uplink
  sample while the speaker plays, in every trial, not only inside the event window (as on real calls).
  Labels are unchanged; inside the old window the samples are identical. Without the flag nothing changes.

AEC residual (--echo-erle-db X, opt-in, needs --echo-continuous, output marked bargein_replay/8):
  the continuous echo is what a linear AEC leaves behind: the echo term of every sample (whole call, event
  windows included) is scaled by 10^(-X/20), i.e. X dB echo-return-loss enhancement on top of the ERL. Noise,
  caller and labels are unchanged; echo_path logs erle_db. X = 0 gives bargein_replay/7 samples exactly.

Arm E2 (--arm-e2, opt-in, output marked bargein_replay/8; arms A-E unchanged): arm E's gate with an unbiased
  online echo-gain estimate: an ordinary least-squares fit near = g x render(L) + a of packet energies over ALL
  render-active packets (hot ones included, until the gate fires), for every lag L in 0..energy_lag_max_ms at
  1 ms steps; the lag with the highest envelope correlation is the matched lag. A hot packet is vetoed when
  near <= 10^(erl2_margin_db/10) x (g x render(L) + a); before erl2_min_updates fits it falls back to
  near <= 10^((erl2_prior_db + erl2_margin_db)/10) x max-window render (arm E's start rule).

Arms F / F2 (--arm-f / --arm-f2, opt-in, output marked bargein_replay/9; arms A-E2 unchanged): an NLMS linear
  echo canceller (adapter render log = reference, uplink = input; 4 kHz low band, cross-correlation delay
  estimate, 80 ms FIR, Geigel double-talk freeze) in front of arm D's gate on the residual (energy_k_db,
  energy_min_speech_ms). F vetoes against the render energy (D's rule); F2 against the capture once the
  canceller's own ERLE monitor says it has converged. Per-trial ERLE (scoring only) is logged. See NlmsGate.

Arms G / G2 (--arm-g / --arm-g2, opt-in, output marked bargein_replay/10; arms A-F2 unchanged): arm F's NLMS
  canceller followed by a residual-only gate: a packet is speech when the residual RMS >= --resid-threshold, with
  NO render-referenced veto; flush after energy_min_speech_ms. The gate is armed (latched) once the canceller has
  placed its filter and its own ERLE monitor reached --resid-arm-db. G2 adds a residual echo suppressor (NLP):
  the residual energy minus the canceller's estimate of its residual echo (echo-estimate energy x 10^(-monitored
  ERLE/10), over-subtracted by --resid-nlp-db). See ResidualGate.

Arm H (--arm-h, opt-in, output marked bargein_replay/11; arms A-G2 unchanged): arm G's residual-only gate after a
  double-talk-robust two-path NLMS canceller instead of the Geigel freeze: a background filter adapts on every
  render-active packet, the foreground filter (read by the gate) copies it when the background residual beats the
  foreground's by --twopath-copy-db for --twopath-copy-packets consecutive packets. See TwoPathGate.

Arm O (--arm-o, opt-in, output marked bargein_replay/12; arms A-H unchanged): arm G unchanged plus a Geigel-free
  shadow NLMS filter used only as a stall/divergence guard: the shadow replaces G's filter only after
  --guard-packets consecutive render-active packets in which G's monitored ERLE is < --guard-erle-db and the shadow
  residual is >= --guard-margin-db lower. Until then every decision is arm G's. See ShadowGuardGate.

Arm Q (--arm-q, needs --arm-o, opt-in, output marked bargein_replay/14; arms A-O unchanged): arm O's canceller and
  residual-speech decision unchanged, but the onset (--energy-min-speech-ms of consecutive residual speech, arm O's
  flush point) only DUCKS playout: the adapter pauses its playout FIFO (reversible, no frame is dropped). The flush is
  COMMITTED once --duck-commit-ms of cumulative residual speech (counting the onset run) is reached within
  --duck-window-ms of the duck; otherwise playout resumes (un-duck) where it stopped and the gate re-arms. For arm Q
  latency_ms is time-to-ducked-or-silent (end of the last audible bot sample; a paused speaker is silent and resumed
  audio counts), false_barge_ins counts COMMITTED flushes on negatives, duck_false_alarms counts ducks that were not
  committed (undone or open at the end). See DuckConfirmGate.

Arm R (--arm-r, needs --arm-q, opt-in, output marked bargein_replay/15; arms A-Q unchanged): arm Q with a separate,
  lower residual RMS threshold --split-duck-rms (<= --resid-threshold) for the onset/DUCK run only; the COMMIT still
  counts only packets with residual RMS >= --resid-threshold. With --split-duck-rms equal to --resid-threshold arm R
  is arm Q. Same latency/false-cut/duck bookkeeping as arm Q. See SplitDuckGate.

Arm S (--arm-s, needs --arm-q, opt-in, output marked bargein_replay/16; arms A-R unchanged): arm Q plus a pre-arm
  speech rule: while the residual gate is NOT yet armed (filter not placed, or its ERLE monitor never reached
  --resid-arm-db), a packet whose residual RMS >= --prearm-rms (>= --resid-threshold) also counts as speech for arm
  Q's onset/duck run and commit count. Once armed (latched) arm S is arm Q. Same latency/false-cut/duck bookkeeping
  as arm Q. See PreArmGate.

Arm T (--arm-t, needs --arm-s, opt-in, output marked bargein_replay/17; arms A-S unchanged): arm S with an
  echo-aware pre-arm rule: while the gate is not yet armed, a packet counts as pre-arm speech only when its residual
  RMS >= --prearm-rms AND its residual energy is >= --prearm-echo-db above the predicted residual echo (the max
  render-window energy over the lag window x an echo-return estimate learned from earlier unarmed, render-active,
  non-speech packets). Once armed, arm Q. See PreArmEchoGate.

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
from math import ceil, cos, exp, log10, pi, sin, sqrt, tanh
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
HARNESS_CONTINUOUS = "bargein_replay/7"  # only with --echo-continuous
HARNESS_AEC = "bargein_replay/8"  # only with --echo-erle-db or --arm-e2
HARNESS_NLMS = "bargein_replay/9"  # only with --arm-f or --arm-f2
HARNESS_RESID = "bargein_replay/10"  # only with --arm-g or --arm-g2
HARNESS_TWOPATH = "bargein_replay/11"  # only with --arm-h
HARNESS_SHADOW = "bargein_replay/12"  # only with --arm-o
HARNESS_DUCK = "bargein_replay/14"  # only with --arm-q (13 was arm P, reverted, not on this line)
HARNESS_SPLIT = "bargein_replay/15"  # only with --arm-r
HARNESS_PREARM = "bargein_replay/16"  # only with --arm-s
HARNESS_PREARM_ECHO = "bargein_replay/17"  # only with --arm-t
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


class ErlFitGate:
    """Arm E2 flush decision (bargein_replay/8): energy-ratio gate with a least-squares echo-gain fit.

    Same inputs as ErlGate: uplink PCM packets at adapter receive time (observe) and the bot PCM frames this
    adapter sent (render). Nothing else: the sim's ERL, ERLE, echo path, labels and onset are never read.
    Per lag L (1 ms steps over 0..energy_lag_max_ms), ref_L = render energy of the packet-length window ending
    L ms before the packet end. Every render-active packet (some render energy in the lag window) updates the
    sums of near, near^2, ref_L, ref_L^2 and near x ref_L while the gate has not fired, hot or not (no
    selection on the uplink level). The matched lag maximises the Pearson correlation of near and ref_L; the
    gain g and intercept a are the OLS fit at that lag; est_db = 10 log10(g) (-100 when g <= 0).
    (a) Echo gate: a hot packet is vetoed when near <= 10^(erl2_margin_db/10) x (g x ref_L + a) once
        erl2_min_updates packets were fitted and g > 0; before that when near <= 10^((erl2_prior_db +
        erl2_margin_db)/10) x the max ref over the window. The decision uses the fit before this packet.
    (b) Minimum speech: flush after erl2_min_speech_ms of consecutive hot, unexplained packets.
    """

    def __init__(self, cfg, span_ms):
        self.margin = 10 ** (cfg.erl2_margin_db / 10)
        self.prior = 10 ** ((cfg.erl2_prior_db + cfg.erl2_margin_db) / 10)
        self.minimum = cfg.erl2_min_speech_ms
        self.min_updates = cfg.erl2_min_updates
        self.lags = cfg.energy_lag_max_ms + 1
        self.sent = pcm(span_ms)
        self.prefix = [0]
        self.n = self.sn = self.snn = 0
        self.sr = [0] * self.lags
        self.srr = [0] * self.lags
        self.snr = [0] * self.lags
        self.g = None
        self.a = 0.0
        self.lag_ms = None
        self.est_db = cfg.erl2_prior_db
        self.updates = 0
        self.run = 0
        self.fired = False
        self.vetoes = 0
        self.max_ratio2 = None

    def render(self, t, frame):
        pos = t * SAMPLES_MS
        self.sent[pos:pos + len(frame)] = frame

    def refs(self, end, n):
        sent, prefix = self.sent, self.prefix
        while len(prefix) <= end:
            v = sent[len(prefix) - 1]
            prefix.append(prefix[-1] + v * v)
        out = []
        for lag in range(self.lags):
            hi = end - lag * SAMPLES_MS
            lo = hi - n
            out.append(prefix[hi] - prefix[lo] if lo >= 0 else 0)
        return out

    def fit(self):
        n, sn, snn = self.n, self.sn, self.snn
        vn = n * snn - sn * sn
        best = None
        for lag in range(self.lags):
            vr = n * self.srr[lag] - self.sr[lag] ** 2
            if vr <= 0 or vn <= 0:
                continue
            cov = n * self.snr[lag] - sn * self.sr[lag]
            r = cov / sqrt(vn * vr)
            if best is None or r > best[0]:
                best = (r, lag, cov / vr)
        if best is None:
            return
        _, self.lag_ms, self.g = best
        self.a = (sn - self.g * self.sr[self.lag_ms]) / n
        self.est_db = 10 * log10(self.g) if self.g > 0 else -100.0

    def observe(self, t, packet):
        end = t * SAMPLES_MS
        n = len(packet)
        refs = self.refs(end, n)
        near = sum(x * x for x in packet)
        speech = hot(packet, THRESHOLD)
        top = max(refs)
        if speech and top:
            self.max_ratio2 = near / top if self.max_ratio2 is None else max(self.max_ratio2, near / top)
            if self.updates >= self.min_updates and self.g is not None and self.g > 0:
                explained = near <= self.margin * (self.g * refs[self.lag_ms] + self.a)
            else:
                explained = near <= self.prior * top
            if explained:
                speech = False
                self.vetoes += 1
        if not self.fired and top:
            self.n += 1
            self.sn += near
            self.snn += near * near
            sr, srr, snr = self.sr, self.srr, self.snr
            for lag, ref in enumerate(refs):
                sr[lag] += ref
                srr[lag] += ref * ref
                snr[lag] += near * ref
            self.updates += 1
            self.fit()
        self.run = self.run + CAPTURE_MS if speech else 0
        if not self.fired and self.run >= self.minimum:
            self.fired = True
            return True
        return False


def lowpass(taps, cutoff):
    """Hamming-windowed sinc lowpass (cutoff in cycles/sample), unity DC gain, mirrored so it is exactly
    linear-phase, coefficients rounded to 9 decimals so libm ulp differences cannot change output bytes."""
    mid = (taps - 1) // 2
    half = []
    for i in range(mid + 1):
        x = i - mid
        s = 2 * cutoff if x == 0 else sin(2 * pi * cutoff * x) / (pi * x)
        half.append(s * (0.54 - 0.46 * cos(2 * pi * i / (taps - 1))))
    h = half + half[-2::-1]
    total = sum(h)
    return tuple(round(v / total, 9) for v in h)


LOWPASS_16K = lowpass(31, 1800 / 16000)  # 16 kHz -> 4 kHz decimation (passband 0-1.8 kHz)
LOWPASS_4K = lowpass(31, 400 / 4000)  # 4 kHz -> 1 kHz decimation for the delay estimator (0-400 Hz)


def decimate(src, dst, upto, h):
    """Append dst[m] = sum_k h[k] src[4m - k] (zero before index 0) for m in len(dst)..upto-1."""
    span = len(h) - 1
    for m in range(len(dst), upto):
        hi = 4 * m + 1
        lo = hi - len(h)
        seg = src[lo:hi] if lo >= 0 else [0] * (-lo) + list(src[0:hi])
        dst.append(sum(map(mul, h, seg)))
    return span


class NlmsGate:
    """Arms F / F2 (bargein_replay/9): NLMS linear echo canceller in front of arm D's energy-ratio gate.

    Same inputs as EnergyGate: uplink PCM packets at adapter receive time (observe) and the bot PCM frames this
    adapter sent (render). Nothing else: the sim's echo path, ERL, delays, labels and onset are never read.
    Both signals are low-passed (31-tap Hamming sinc, 0-1.8 kHz) and decimated to 4 kHz, in adapter time.
    (1) Delay estimator: normalised cross-correlation of capture and render at 1 kHz (a second 0-400 Hz
        lowpass) accumulated over render-active, non-double-talk packets, lags 0..energy_lag_max_ms; the peak
        lag is the bulk delay once nlms_min_updates packets were accumulated.
    (2) NLMS FIR (4 kHz, taps covering lags delay - nlms_pre_ms .. delay + nlms_post_ms): y = w.x,
        e = capture - y, w += mu e x / (x.x + delta), delta = taps x nlms_delta. When the delay estimate moves,
        the weights are shifted with it (each tap keeps its absolute lag).
    (3) Geigel double-talk detector per packet: max|capture| >= nlms_geigel x max|render| over the filter's
        lag span (whole 0..lag window before the delay is known); flagged packets plus nlms_hang_ms after them
        freeze adaptation and the delay estimator. Filtering continues.
    (4) Gate on the residual e (4 kHz packet energy): hot when RMS >= the VAD threshold; arm F ("render")
        vetoes when e energy <= 10^(energy_k_db/10) x the max render-window energy over 0..energy_lag_max_ms
        (arm D's rule, on the residual); arm F2 ("echo") uses D's rule until the canceller's own ERLE monitor
        (EWMA 0.1 of capture/residual dB over adapting packets) reaches nlms_converged_db, then vetoes when the
        canceller removed at least nlms_margin_db of the packet's capture energy (e <= d x 10^(-margin/10)).
        Flush after energy_min_speech_ms of consecutive hot, unvetoed packets.
    """

    def __init__(self, cfg, span_ms, ref):
        assert ref in ("render", "echo")
        self.ref = ref
        self.mu = cfg.nlms_mu
        self.pre = cfg.nlms_pre_ms * 4
        self.length = (cfg.nlms_pre_ms + cfg.nlms_post_ms) * 4
        self.delta = cfg.nlms_delta * self.length
        self.geigel = cfg.nlms_geigel
        self.hang = cfg.nlms_hang_ms
        self.min_updates = cfg.nlms_min_updates
        self.ratio2 = 10 ** (cfg.energy_k_db / 10)
        self.minimum = cfg.energy_min_speech_ms
        self.lag_ms = cfg.energy_lag_max_ms
        self.sent = pcm(span_ms)
        self.near = pcm(span_ms)
        self.r4, self.d4, self.r1, self.d1 = [], [], [], []
        self.p4 = [0.0]  # prefix sums of r4^2
        self.p1 = [0.0]  # prefix sums of r1^2
        self.corr = [0.0] * (self.lag_ms + 1)
        self.rr = [0.0] * (self.lag_ms + 1)
        self.w = [0.0] * self.length
        self.delay_ms = None
        self.est_updates = 0
        self.updates = 0
        self.dt_packets = 0
        self.dt_until = -1
        self.erle_hat_db = 0.0  # EWMA (0.1) of 10 log10(capture / residual) over adapting packets
        self.converged_db = cfg.nlms_converged_db  # F2 trusts the canceller once its monitored ERLE reaches this
        self.margin_db = cfg.nlms_margin_db
        self.log = []  # (t, capture energy, residual energy) per render-active packet before the fire
        self.run = 0
        self.fired = False
        self.vetoes = 0

    def render(self, t, frame):
        pos = t * SAMPLES_MS
        self.sent[pos:pos + len(frame)] = frame

    def advance(self, end):
        m1, q1 = end // 4, end // SAMPLES_MS
        decimate(self.sent, self.r4, m1, LOWPASS_16K)
        decimate(self.near, self.d4, m1, LOWPASS_16K)
        decimate(self.r4, self.r1, q1, LOWPASS_4K)
        decimate(self.d4, self.d1, q1, LOWPASS_4K)
        for src, prefix in ((self.r4, self.p4), (self.r1, self.p1)):
            while len(prefix) <= len(src):
                v = src[len(prefix) - 1]
                prefix.append(prefix[-1] + v * v)

    def estimate(self, q0, q1):
        r1, p1, corr, rr = self.r1, self.p1, self.corr, self.rr
        d = self.d1[q0:q1]
        for lag in range(self.lag_ms + 1):
            lo, hi = q0 - lag, q1 - lag
            if hi <= 0:
                break
            seg = r1[lo:hi] if lo >= 0 else [0.0] * (-lo) + r1[0:hi]
            corr[lag] += sum(map(mul, d, seg))
            rr[lag] += p1[hi] - p1[max(0, lo)]
        self.est_updates += 1
        if self.est_updates < self.min_updates:
            return
        best = None
        for lag in range(self.lag_ms + 1):
            if rr[lag] > 0 and corr[lag] > 0:
                score = corr[lag] / sqrt(rr[lag])
                if best is None or score > best[0]:
                    best = (score, lag)
        if best is None or best[1] == self.delay_ms:
            return
        if self.delay_ms is not None:  # keep every tap at its absolute lag
            shift = (best[1] - self.delay_ms) * 4
            old = self.w
            self.w = [old[j - shift] if 0 <= j - shift < self.length else 0.0 for j in range(self.length)]
        self.delay_ms = best[1]

    def observe(self, t, packet):
        if self.fired:
            return False
        end = t * SAMPLES_MS
        self.near[end - len(packet):end] = packet
        self.advance(end)
        m1 = end // 4
        m0 = m1 - len(packet) // 4
        n = m1 - m0
        r4, d4, p4 = self.r4, self.d4, self.p4
        lag4 = self.lag_ms * 4
        top = max((p4[e] - p4[e - n] for e in range(max(n, m1 - lag4), m1 + 1)), default=0.0)
        active = top >= n * THRESHOLD ** 2
        if self.delay_ms is None:
            lo_lag, hi_lag = 0, lag4
        else:
            lo_lag = max(0, self.delay_ms * 4 - self.pre)
            hi_lag = lo_lag + self.length - 1
        span = r4[max(0, m0 - hi_lag):max(0, m1 - lo_lag)]
        rmax = max(map(abs, span), default=0.0)
        if active and rmax and max(map(abs, d4[m0:m1])) >= self.geigel * rmax:
            self.dt_until = t + self.hang
        dt = active and t <= self.dt_until
        self.dt_packets += dt
        adapt = active and not dt
        ee = ey = 0.0
        if self.delay_ms is None:
            for m in range(m0, m1):
                ee += d4[m] * d4[m]
        else:
            s0 = max(0, self.delay_ms * 4 - self.pre)
            taps, mu, delta = self.length, self.mu, self.delta
            for m in range(m0, m1):
                hi = m - s0 + 1
                lo = hi - taps
                x = r4[lo:hi] if lo >= 0 else [0.0] * (-lo) + r4[0:hi] if hi > 0 else [0.0] * taps
                w = self.w
                y = sum(map(mul, w, x))
                e = d4[m] - y
                ee += e * e
                ey += y * y
                if adapt:
                    norm = p4[max(0, hi)] - p4[max(0, lo)]
                    if norm:
                        g = mu * e / (norm + delta)
                        self.w = [a + g * b for a, b in zip(w, x)]
                        self.updates += 1
        if adapt:
            q1 = end // SAMPLES_MS
            self.estimate(q1 - len(packet) // SAMPLES_MS, q1)
        ed = sum(d4[m] * d4[m] for m in range(m0, m1))
        if active:
            self.log.append((t, ed, ee))
        if adapt and self.delay_ms is not None and ed and ee:  # the canceller's own ERLE monitor
            self.erle_hat_db = 0.9 * self.erle_hat_db + 0.1 * 10 * log10(ed / ee)
        speech = self.decide(n, ee, ed, ey, top)
        self.run = self.run + CAPTURE_MS if speech else 0
        if self.run >= self.minimum:
            self.fired = True
            return True
        return False

    def decide(self, n, ee, ed, ey, top):
        """Is this packet unexplained speech? n samples, residual / capture / echo-estimate energies (4 kHz) and
        the max render-window energy over the lag window."""
        speech = ee >= n * THRESHOLD ** 2
        if speech:
            if self.ref == "echo" and self.erle_hat_db >= self.converged_db:
                explained = ee <= ed * 10 ** (-self.margin_db / 10)  # the canceller removed >= margin dB
            else:
                explained = top and ee <= self.ratio2 * top
            if explained:
                speech = False
                self.vetoes += 1
        return speech


class ResidualGate(NlmsGate):
    """Arms G / G2 (bargein_replay/10): arm F's NLMS canceller (inherited unchanged) + a residual-only gate.

    No veto of any kind: the decision reads only the canceller residual (and, for G2, the canceller's own echo
    estimate and ERLE monitor), never the render energy. Same adapter-only inputs as NlmsGate.
    (1) Arming: the gate cannot fire until the canceller has placed its filter (delay estimate) AND its own ERLE
        monitor has reached resid_arm_db once; then it stays armed (latched) for the rest of the call.
    (2) G ("residual"): a packet is speech when the residual RMS (4 kHz) >= resid_threshold.
    (3) G2 ("nlp"): residual echo suppressor first: level = residual energy - 10^(resid_nlp_db/10) x echo-estimate
        energy x 10^(-monitored ERLE/10) (the canceller's estimate of the echo it left behind); speech when
        level >= resid_threshold^2 per sample.
    Flush after energy_min_speech_ms of consecutive speech packets (inherited).
    """

    def __init__(self, cfg, span_ms, ref):
        assert ref in ("residual", "nlp")
        super().__init__(cfg, span_ms, "render")
        self.ref = ref
        self.level2 = cfg.resid_threshold ** 2
        self.arm_db = cfg.resid_arm_db
        self.nlp = 10 ** (cfg.resid_nlp_db / 10)
        self.armed = False

    def decide(self, n, ee, ed, ey, top):
        if not self.armed:
            self.armed = self.delay_ms is not None and self.erle_hat_db >= self.arm_db
            if not self.armed:
                return False
        level = ee
        if self.ref == "nlp":
            level = ee - self.nlp * ey * 10 ** (-self.erle_hat_db / 10)
        return level >= n * self.level2


class TwoPathGate(ResidualGate):
    """Arm H (bargein_replay/11): arm G's residual-only gate after a double-talk-robust two-path NLMS canceller.

    Same adapter-only inputs as NlmsGate; the 4 kHz front end, delay estimator, filter span, step size and
    regularisation are NlmsGate's. The one change is the adaptation control (no Geigel detector at all):
    (1) Background filter wb: NLMS (mu, delta) on EVERY render-active packet, double talk or not; its residual
        is never used for the decision.
    (2) Foreground filter w: never adapts itself. Its residual feeds the gate, the ERLE monitor and the log.
        After each render-active packet (filter placed), if the background residual energy is at least
        twopath_copy_db below the foreground's AND below the capture energy, a counter increments (else it resets);
        after twopath_copy_packets consecutive such packets the background is copied into the foreground.
        Double talk disturbs the background, which then loses to the foreground and is not copied.
    (3) The delay estimator updates on every render-active packet (no double-talk freeze).
    (4) The ERLE monitor (arming) is the EWMA 0.1 of capture/foreground-residual dB over render-active packets.
    Gate: ResidualGate's (armed once placed and the monitor reached resid_arm_db; residual RMS >= resid_threshold
    for energy_min_speech_ms).
    """

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms, "residual")
        self.copy_ratio = 10 ** (-cfg.twopath_copy_db / 10)
        self.copy_packets = cfg.twopath_copy_packets
        self.wb = [0.0] * self.length
        self.better = 0
        self.copies = 0

    def estimate(self, q0, q1):
        before = self.delay_ms
        super().estimate(q0, q1)  # shifts the foreground w with a delay move
        if before is not None and self.delay_ms != before:
            shift = (self.delay_ms - before) * 4
            old = self.wb
            self.wb = [old[j - shift] if 0 <= j - shift < self.length else 0.0 for j in range(self.length)]

    def observe(self, t, packet):
        if self.fired:
            return False
        end = t * SAMPLES_MS
        self.near[end - len(packet):end] = packet
        self.advance(end)
        m1 = end // 4
        m0 = m1 - len(packet) // 4
        n = m1 - m0
        r4, d4, p4 = self.r4, self.d4, self.p4
        lag4 = self.lag_ms * 4
        top = max((p4[e] - p4[e - n] for e in range(max(n, m1 - lag4), m1 + 1)), default=0.0)
        active = top >= n * THRESHOLD ** 2
        ee = ey = eb = 0.0
        if self.delay_ms is None:
            for m in range(m0, m1):
                ee += d4[m] * d4[m]
        else:
            s0 = max(0, self.delay_ms * 4 - self.pre)
            taps, mu, delta = self.length, self.mu, self.delta
            w = self.w
            for m in range(m0, m1):
                hi = m - s0 + 1
                lo = hi - taps
                x = r4[lo:hi] if lo >= 0 else [0.0] * (-lo) + r4[0:hi] if hi > 0 else [0.0] * taps
                y = sum(map(mul, w, x))
                e = d4[m] - y
                ee += e * e
                ey += y * y
                wb = self.wb
                b = d4[m] - sum(map(mul, wb, x))
                eb += b * b
                if active:
                    norm = p4[max(0, hi)] - p4[max(0, lo)]
                    if norm:
                        g = mu * b / (norm + delta)
                        self.wb = [a + g * v for a, v in zip(wb, x)]
                        self.updates += 1
        if active:
            q1 = end // SAMPLES_MS
            self.estimate(q1 - len(packet) // SAMPLES_MS, q1)
        ed = sum(d4[m] * d4[m] for m in range(m0, m1))
        if active:
            self.log.append((t, ed, ee))
            if self.delay_ms is not None and ed and ee:
                self.erle_hat_db = 0.9 * self.erle_hat_db + 0.1 * 10 * log10(ed / ee)
        speech = self.decide(n, ee, ed, ey, top)
        if active and self.delay_ms is not None:  # copy-on-better, after this packet's decision
            self.better = self.better + 1 if eb <= self.copy_ratio * ee and eb < ed else 0
            if self.better >= self.copy_packets:
                self.w = list(self.wb)
                self.copies += 1
                self.better = 0
        self.run = self.run + CAPTURE_MS if speech else 0
        if self.run >= self.minimum:
            self.fired = True
            return True
        return False


class ShadowGuardGate(ResidualGate):
    """Arm O (bargein_replay/12): arm G unchanged (Geigel-frozen NLMS + residual-only gate) plus a Geigel-free
    shadow filter that is used ONLY as a stall/divergence guard.

    Same adapter-only inputs as NlmsGate. The foreground path (filter w, Geigel freeze, delay estimator, ERLE
    monitor, gate) is NlmsGate.observe + ResidualGate.decide, arithmetic unchanged, so until the guard fires every
    decision is arm G's.
    (1) Shadow filter ws: NLMS (same mu, delta, span, delay placement as w; shifted with every delay move) adapting
        on EVERY render-active packet once the filter is placed, double talk or not. Its residual never feeds the
        gate. Its own monitor is the EWMA 0.1 of capture/shadow-residual dB over render-active packets.
    (2) Guard, evaluated after each render-active packet's decision (filter placed): the foreground is "stalled"
        while its monitored ERLE is < guard_erle_db; the packet counts when the foreground is stalled AND the shadow
        residual energy is at least guard_margin_db below the foreground's. After guard_packets consecutive counting
        packets, the shadow replaces the foreground (w = ws, and the foreground monitor takes the shadow monitor's
        value, since the residual it describes is now the shadow's); the counter resets.
    """

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms, "residual")
        self.guard_erle_db = cfg.guard_erle_db
        self.guard_ratio = 10 ** (-cfg.guard_margin_db / 10)
        self.guard_packets = cfg.guard_packets
        self.ws = [0.0] * self.length
        self.shadow_erle_db = 0.0
        self.guard_run = 0
        self.swaps = 0

    def estimate(self, q0, q1):
        before = self.delay_ms
        super().estimate(q0, q1)  # shifts the foreground w with a delay move (NlmsGate)
        if before is not None and self.delay_ms != before:
            shift = (self.delay_ms - before) * 4
            old = self.ws
            self.ws = [old[j - shift] if 0 <= j - shift < self.length else 0.0 for j in range(self.length)]

    def observe(self, t, packet):
        if self.fired:
            return False
        end = t * SAMPLES_MS
        self.near[end - len(packet):end] = packet
        self.advance(end)
        m1 = end // 4
        m0 = m1 - len(packet) // 4
        n = m1 - m0
        r4, d4, p4 = self.r4, self.d4, self.p4
        lag4 = self.lag_ms * 4
        top = max((p4[e] - p4[e - n] for e in range(max(n, m1 - lag4), m1 + 1)), default=0.0)
        active = top >= n * THRESHOLD ** 2
        if self.delay_ms is None:
            lo_lag, hi_lag = 0, lag4
        else:
            lo_lag = max(0, self.delay_ms * 4 - self.pre)
            hi_lag = lo_lag + self.length - 1
        span = r4[max(0, m0 - hi_lag):max(0, m1 - lo_lag)]
        rmax = max(map(abs, span), default=0.0)
        if active and rmax and max(map(abs, d4[m0:m1])) >= self.geigel * rmax:
            self.dt_until = t + self.hang
        dt = active and t <= self.dt_until
        self.dt_packets += dt
        adapt = active and not dt
        ee = ey = es = 0.0
        placed = self.delay_ms is not None
        if not placed:
            for m in range(m0, m1):
                ee += d4[m] * d4[m]
        else:
            s0 = max(0, self.delay_ms * 4 - self.pre)
            taps, mu, delta = self.length, self.mu, self.delta
            for m in range(m0, m1):
                hi = m - s0 + 1
                lo = hi - taps
                x = r4[lo:hi] if lo >= 0 else [0.0] * (-lo) + r4[0:hi] if hi > 0 else [0.0] * taps
                w = self.w
                y = sum(map(mul, w, x))
                e = d4[m] - y
                ee += e * e
                ey += y * y
                norm = p4[max(0, hi)] - p4[max(0, lo)]
                if adapt:
                    if norm:
                        g = mu * e / (norm + delta)
                        self.w = [a + g * b for a, b in zip(w, x)]
                        self.updates += 1
                ws = self.ws  # shadow: every render-active packet, no double-talk freeze
                b = d4[m] - sum(map(mul, ws, x))
                es += b * b
                if active and norm:
                    g = mu * b / (norm + delta)
                    self.ws = [a + g * v for a, v in zip(ws, x)]
        if adapt:
            q1 = end // SAMPLES_MS
            self.estimate(q1 - len(packet) // SAMPLES_MS, q1)
        ed = sum(d4[m] * d4[m] for m in range(m0, m1))
        if active:
            self.log.append((t, ed, ee))
        if adapt and self.delay_ms is not None and ed and ee:  # the foreground's own ERLE monitor (arm G)
            self.erle_hat_db = 0.9 * self.erle_hat_db + 0.1 * 10 * log10(ed / ee)
        speech = self.decide(n, ee, ed, ey, top)
        if active and placed:  # stall/divergence guard, after this packet's decision
            if ed and es:
                self.shadow_erle_db = 0.9 * self.shadow_erle_db + 0.1 * 10 * log10(ed / es)
            stalled = self.erle_hat_db < self.guard_erle_db
            self.guard_run = self.guard_run + 1 if stalled and es <= self.guard_ratio * ee else 0
            if self.guard_run >= self.guard_packets:
                self.w = list(self.ws)
                self.erle_hat_db = self.shadow_erle_db
                self.swaps += 1
                self.guard_run = 0
        self.run = self.run + CAPTURE_MS if speech else 0
        if self.run >= self.minimum:
            self.fired = True
            return True
        return False


class DuckConfirmGate(ShadowGuardGate):
    """Arm Q (bargein_replay/14): arm O unchanged (canceller, shadow stall guard, arming, residual-only speech
    decision), with the onset split from the decision. observe() returns None or an event:
      "duck"   energy_min_speech_ms of consecutive residual speech (arm O's flush point): pause playout;
      "commit" duck_commit_ms of cumulative residual speech since the onset run began (the onset run counts, gaps
               do not reset it) within duck_window_ms of the duck: flush playout (terminal);
      "unduck" the window elapsed without a commit: resume playout; the onset run restarts from zero.
    With duck_commit_ms <= energy_min_speech_ms the onset commits at once ("commit", no duck): arm O exactly.
    """

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms)
        self.onset_ms = self.minimum  # arm O's flush run (energy_min_speech_ms)
        self.minimum = float("inf")  # the inherited run never fires; step() decides
        self.commit_ms = cfg.duck_commit_ms
        self.window_ms = cfg.duck_window_ms
        self.ducked = False
        self.onset_run = 0
        self.acc = 0
        self.duck_t = None
        self.ducks = 0
        self.unducks = 0
        self.speech = False

    def decide(self, n, ee, ed, ey, top):
        self.speech = super().decide(n, ee, ed, ey, top)
        return self.speech

    def observe(self, t, packet):
        if self.fired:
            return None
        self.speech = False
        super().observe(t, packet)
        return self.step(t, self.speech)

    def step(self, t, speech):
        if self.fired:  # committed: terminal
            return None
        if not self.ducked:
            self.onset_run = self.onset_run + CAPTURE_MS if speech else 0
            if self.onset_run < self.onset_ms:
                return None
            self.ducked, self.duck_t, self.acc = True, t, self.onset_run
            self.ducks += 1
            if self.acc >= self.commit_ms:
                self.fired = True
                return "commit"
            return "duck"
        self.acc += CAPTURE_MS if speech else 0
        if self.acc >= self.commit_ms:
            self.fired = True
            return "commit"
        if t - self.duck_t >= self.window_ms:
            self.ducked, self.onset_run = False, 0
            self.unducks += 1
            return "unduck"
        return None


class SplitDuckGate(DuckConfirmGate):
    """Arm R (bargein_replay/15): arm Q unchanged (canceller, shadow guard, arming, duck/commit/un-duck, window)
    except the onset/DUCK run reads a separate, lower residual threshold split_duck_rms: a packet is duck-speech when
    the gate is armed and the residual RMS (4 kHz) >= split_duck_rms. The COMMIT counts only arm Q's speech
    (residual RMS >= resid_threshold): at the duck, the commit count starts at the strong-speech ms inside the onset
    run, and after the duck only strong packets add to it. With split_duck_rms == resid_threshold, arm Q exactly.
    """

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms)
        self.duck_level2 = cfg.split_duck_rms ** 2
        self.duck_speech = False
        self.onset_strong = 0

    def decide(self, n, ee, ed, ey, top):
        speech = super().decide(n, ee, ed, ey, top)  # arm Q's (O's) decision; also latches arming
        self.duck_speech = self.armed and ee >= n * self.duck_level2
        return speech

    def observe(self, t, packet):
        if self.fired:
            return None
        self.speech = self.duck_speech = False
        ShadowGuardGate.observe(self, t, packet)
        return self.step(t, self.speech, self.duck_speech)

    def step(self, t, speech, duck_speech=None):
        if self.fired:  # committed: terminal
            return None
        duck_speech = speech if duck_speech is None else (duck_speech or speech)
        if not self.ducked:
            if duck_speech:
                self.onset_run += CAPTURE_MS
                self.onset_strong += CAPTURE_MS if speech else 0
            else:
                self.onset_run = self.onset_strong = 0
            if self.onset_run < self.onset_ms:
                return None
            self.ducked, self.duck_t, self.acc = True, t, self.onset_strong
            self.ducks += 1
            if self.acc >= self.commit_ms:
                self.fired = True
                return "commit"
            return "duck"
        self.acc += CAPTURE_MS if speech else 0
        if self.acc >= self.commit_ms:
            self.fired = True
            return "commit"
        if t - self.duck_t >= self.window_ms:
            self.ducked, self.onset_run, self.onset_strong = False, 0, 0
            self.unducks += 1
            return "unduck"
        return None


class PreArmGate(DuckConfirmGate):
    """Arm S (bargein_replay/16): arm Q unchanged (canceller, shadow guard, arming, residual speech rule, duck/commit/
    un-duck, window) plus ONE pre-arm rule: while the gate is not yet armed (the filter is not placed or its ERLE
    monitor never reached resid_arm_db; arming is latched), a packet whose residual RMS (4 kHz; before placement the
    capture itself) >= prearm_rms also counts as speech for the onset/duck run and the commit count. Once armed,
    arm Q exactly. With prearm_rms above every packet, arm Q exactly.
    """

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms)
        self.prearm_level2 = cfg.prearm_rms ** 2
        self.prearm_packets = 0

    def decide(self, n, ee, ed, ey, top):
        speech = super().decide(n, ee, ed, ey, top)  # arm Q's decision; also latches arming; sets self.speech
        if not speech and not self.armed and ee >= n * self.prearm_level2:
            self.speech = speech = True
            self.prearm_packets += 1
        return speech


class PreArmEchoGate(PreArmGate):
    """Arm T (bargein_replay/17): arm S unchanged except that the pre-arm rule is echo-aware. While the gate is not
    yet armed, a packet counts as pre-arm speech only when its residual RMS >= prearm_rms (arm S's level) AND its
    residual energy exceeds the PREDICTED residual echo by prearm_echo_db: ee >= 10^(prearm_echo_db/10) x g x top,
    where top is the max render-window energy over the lag window (what the adapter played that can still echo) and
    g is the echo-return estimate sum(ee) / sum(top) over the unarmed, render-active (top >= the VAD level) packets
    so far that were not counted as speech, starting at the first such packet whose capture reached the VAD level
    (render-active packets before it only show the bulk delay, not the echo return). Loud pre-convergence echo (high g) then no longer passes for caller
    speech, while with a quiet echo path (low g) the rule is close to arm S's. Before any such packet g = 1 (the echo
    is assumed as loud as the render until measured); with the render silent (top 0) the rule is arm S's. Once armed,
    arm Q. With prearm_echo_db low enough that the echo term never binds, arm S exactly.
    """

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms)
        self.prearm_margin = 10 ** (cfg.prearm_echo_db / 10)
        self.echo_resid = 0.0  # sum of residual energy over the echo-learning packets
        self.echo_top = 0.0  # sum of render-window energy over the same packets
        self.heard = False  # a render-active capture reached the VAD level (the echo path has started to return)

    def decide(self, n, ee, ed, ey, top):
        speech = DuckConfirmGate.decide(self, n, ee, ed, ey, top)  # arm Q's decision; latches arming
        if speech or self.armed:
            return speech
        if ee >= n * self.prearm_level2:
            g = self.echo_resid / self.echo_top if self.echo_top else 1.0  # echo return; 1 until measured
            predicted = self.prearm_margin * g * top
            if ee >= predicted:
                self.speech = speech = True
                self.prearm_packets += 1
                return speech
        if top >= n * THRESHOLD ** 2:  # render-active, unarmed, not speech: learn the echo return ...
            self.heard = self.heard or ee >= n * THRESHOLD ** 2
            if self.heard:  # ... once the capture was first hot (before that the echo has not arrived yet)
                self.echo_resid += ee
                self.echo_top += top
        return speech


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
    erle = getattr(cfg, "echo_erle_db", None)
    if erle:  # bargein_replay/8: linear-AEC residual, X dB ERLE on the echo term only (0/None: /7 samples)
        gain = gain * 10 ** (-erle / 20)
    if getattr(cfg, "echo_continuous", False):
        # bargein_replay/7: the same echo path in every sample (echo whenever the speaker plays), not only in
        # the event window. tanh is cached per speaker sample; each echo value is the identical float sum.
        taps = r["taps"]
        lo = max(0, start - bulk - max(d for d, _ in taps))
        hi = min(len(speaker), start + size - bulk)
        soft = {ref: tanh(speaker[ref] / level) for ref in range(lo, hi)}
        for j in range(size):
            i = start + j
            offset = i - origin
            value = mic[offset] if mic is not None and 0 <= offset < len(mic) else 0
            echo = 0.0
            for d, h in taps:
                ref = i - bulk - d
                if 0 <= ref < len(speaker):
                    echo += h * level * soft[ref]
            value += gain * echo
            if i < len(noise):
                value += noise[i]
            packet[j] = max(-32768, min(32767, round(value)))
        return packet
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
    fgate = ErlFitGate(cfg, len(speaker) // SAMPLES_MS) if arm == "E2" else None
    ngate = (NlmsGate(cfg, len(speaker) // SAMPLES_MS, "render" if arm == "F" else "echo")
             if arm in ("F", "F2") else
             ResidualGate(cfg, len(speaker) // SAMPLES_MS, "residual" if arm == "G" else "nlp")
             if arm in ("G", "G2") else
             TwoPathGate(cfg, len(speaker) // SAMPLES_MS) if arm == "H" else
             ShadowGuardGate(cfg, len(speaker) // SAMPLES_MS) if arm == "O" else
             DuckConfirmGate(cfg, len(speaker) // SAMPLES_MS) if arm == "Q" else
             SplitDuckGate(cfg, len(speaker) // SAMPLES_MS) if arm == "R" else
             PreArmGate(cfg, len(speaker) // SAMPLES_MS) if arm == "S" else
             PreArmEchoGate(cfg, len(speaker) // SAMPLES_MS) if arm == "T" else None)
    paused = False  # arm Q: playout FIFO paused (ducked); never set for other arms

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
            if queue and not paused:
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
                if fgate:
                    fgate.render(t, bot[first:first + size])
                if ngate:
                    ngate.render(t, bot[first:first + size])
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
            if fgate and fgate.observe(t, payload):
                trace["erl2_fire_ms"] = t
                flush(t, "erl2_gated_local_vad")
            fire = ngate.observe(t, payload) if ngate else False
            if fire == "duck":  # arm Q: pause playout (reversible)
                paused = True
                trace.setdefault("duck_ms", []).append(t)
            elif fire == "unduck":  # arm Q: resume playout where it stopped
                paused = False
                trace.setdefault("unduck_ms", []).append(t)
            elif fire:
                trace["nlms_fire_ms"] = t
                flush(t, "residual_gated_local_vad" if arm in ("G", "G2", "O", "Q", "R", "S", "T") else
                      "twopath_residual_gated_local_vad" if arm == "H" else "nlms_gated_local_vad")
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
    if fgate:
        trace["erl2_vetoes"] = fgate.vetoes
        trace["erl2_updates"] = fgate.updates
        trace["erl2_final_est_db"] = round(fgate.est_db, 2)
        trace["erl2_lag_ms"] = fgate.lag_ms
        trace["energy_max_ratio_db"] = (None if fgate.max_ratio2 is None
                                        else round(10 * log10(fgate.max_ratio2), 2))
    erle = None
    if ngate:
        # ERLE of the canceller (scoring only, uses the label onset): 10 log10(capture / residual energy) per
        # render-active 4 kHz packet whose samples all precede the caller mic onset (no near-end talker).
        hot_e = CAPTURE_MS * 4 * THRESHOLD ** 2  # 4 kHz packet whose capture is above the VAD threshold
        erle = [round(10 * log10(c / r), 2) for t, c, r in ngate.log
                if t - trial["up"] <= onset and c >= hot_e and r]
        trace["nlms_vetoes"] = ngate.vetoes
        trace["nlms_updates"] = ngate.updates
        trace["nlms_dt_packets"] = ngate.dt_packets
        trace["nlms_active_packets"] = len(ngate.log)
        trace["nlms_delay_ms"] = ngate.delay_ms
        trace["nlms_delay_true_ms"] = trial["up"] + trial["down"] + hold + (
            trial["residual"]["delay_ms"] if "residual" in trial else 35)  # direct-path lag, scoring only
        trace["nlms_erle_db"] = median(erle) if erle else None
        trace["nlms_erle_packets"] = len(erle)
        if arm == "H":
            trace["nlms_copies"] = ngate.copies
        if arm in ("O", "Q", "R", "S", "T"):
            trace["nlms_swaps"] = ngate.swaps
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
        if getattr(cfg, "echo_erle_db", None) is not None:
            row["echo_path"]["erle_db"] = cfg.echo_erle_db
    if "caller_gain_db" in trial:
        row["caller_gain_db"] = round(trial["caller_gain_db"], 3)
    if erle is not None:
        row["_erle"] = erle
    if arm in ("S", "T"):
        row["_prearm"] = ngate.prearm_packets  # popped by run(): arm S summary only  # popped by run(): pooled packet ERLE summary
    if "bot" in trial:
        row["bot_render"] = dict(clip=trial["bot_clip"], offset=trial["bot_offset"], mic_clip=trial["mic_clip"])
    if arm in ("Q", "R", "S", "T"):  # bargein_replay/14 duck bookkeeping (adapter clock; /15 R, /16 S, /17 T the same)
        ducks, unducks = trace.get("duck_ms", []), trace.get("unduck_ms", [])
        row["ducks"] = len(ducks)
        row["duck_false_alarms"] = len(ducks) - (1 if muted and ducks else 0)  # ducks not committed
        row["duck_ms_total"] = sum(u - d for d, u in zip(ducks, unducks))  # paused time of undone ducks
        row["duck_open_at_end"] = len(ducks) > len(unducks) and not muted
        row["commit_latency_ms"] = (trace["adapter_flush_ms"] - onset
                                    if kind == "valid" and muted else None)
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
    arm_e2 = getattr(cfg, "arm_e2", False)
    arm_f = getattr(cfg, "arm_f", False)
    arm_f2 = getattr(cfg, "arm_f2", False)
    arm_g = getattr(cfg, "arm_g", False)
    arm_g2 = getattr(cfg, "arm_g2", False)
    arm_h = getattr(cfg, "arm_h", False)
    arm_o = getattr(cfg, "arm_o", False)
    arm_q = getattr(cfg, "arm_q", False)
    arm_r = getattr(cfg, "arm_r", False)
    arm_s = getattr(cfg, "arm_s", False)
    arm_t = getattr(cfg, "arm_t", False)
    nlms = arm_f or arm_f2 or arm_g or arm_g2 or arm_h or arm_o
    result = dict(seed=seed, trials_per_arm=n, parameters={
        k: v for k, v in vars(cfg).items()
        if k not in ("aa", "output", "seed", "trials", "arm_c", "arm_d", "arm_e", "arm_e2", "arm_f", "arm_f2",
                     "arm_g", "arm_g2", "arm_h", "arm_o", "arm_q", "arm_r", "arm_s", "arm_t")
        and (arm_r or not k.startswith("split_"))  # bargein_replay/15 only with --arm-r
        and (arm_s or not k.startswith("prearm_"))  # bargein_replay/16 only with --arm-s
        and (arm_t or k != "prearm_echo_db")  # bargein_replay/17 only with --arm-t
        and (arm_c or not k.startswith("gate_"))  # default run keeps the frozen bytes
        and (arm_d or arm_e or arm_e2 or nlms or not k.startswith("energy_"))
        and (nlms or not k.startswith("nlms_"))  # bargein_replay/9 only with --arm-f / --arm-f2 (/10: G, G2)
        and (arm_g or arm_g2 or arm_h or arm_o or not k.startswith("resid_"))  # /10 G, G2 (/11: H, /12: O)
        and (arm_h or not k.startswith("twopath_"))  # bargein_replay/11 only with --arm-h
        and (arm_o or not k.startswith("guard_"))  # bargein_replay/12 only with --arm-o
        and (arm_q or not k.startswith("duck_"))  # bargein_replay/14 only with --arm-q
        and (arm_e2 or not k.startswith("erl2_"))  # bargein_replay/8 only with --arm-e2
        and (arm_e or not k.startswith("erl_") or k == "erl_db_range")  # erl_db_range: bargein_replay/5 stress
        and (k != "echo_model" or echo_model != "copy")
        and (k != "bot_render" or recorded)
        and (k not in ("caller_level_db_range", "erl_db_range") or v is not None)
        and (k != "echo_continuous" or v)  # bargein_replay/7 only when on
        and (k != "echo_erle_db" or v is not None)  # bargein_replay/8 only when set
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
    if getattr(cfg, "echo_continuous", False):
        result["harness"] = HARNESS_CONTINUOUS
    if getattr(cfg, "echo_erle_db", None) is not None or arm_e2:
        result["harness"] = HARNESS_AEC
    if arm_f or arm_f2:
        result["harness"] = HARNESS_NLMS
    if arm_g or arm_g2:
        result["harness"] = HARNESS_RESID
    if arm_h:
        result["harness"] = HARNESS_TWOPATH
    if arm_o:
        result["harness"] = HARNESS_SHADOW
    if arm_q:
        result["harness"] = HARNESS_DUCK
    if arm_r:
        result["harness"] = HARNESS_SPLIT
    if arm_s:
        result["harness"] = HARNESS_PREARM
    if arm_t:
        result["harness"] = HARNESS_PREARM_ECHO
    arms = ("A", "B", "C") if arm_c else ("A", "B")
    arms += (("D",) if arm_d else ()) + (("E",) if arm_e else ()) + (("E2",) if arm_e2 else ())
    arms += (("F",) if arm_f else ()) + (("F2",) if arm_f2 else ())
    arms += (("G",) if arm_g else ()) + (("G2",) if arm_g2 else ()) + (("H",) if arm_h else ())
    for arm in (arms + (("O",) if arm_o else ()) + (("Q",) if arm_q else ()) + (("R",) if arm_r else ()) + (("S",) if arm_s else ())
                + (("T",) if arm_t else ())):
        prearm_packets = 0
        rows = []
        for case in cases:
            rows.append(replay(case, arm, cfg, bot))
            prearm_packets += rows[-1].pop("_prearm", 0)
        pooled = [v for row in rows for v in row.pop("_erle", ())]
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
        if arm == "E2":
            summary = result["arms"][arm]
            summary["double_talk_latency_ms"] = percentiles(
                [row["latency_ms"] for row in valid if row.get("echo_path", {}).get("double_talk")])
            summary["valid_trials_with_veto"] = sum(row["trace"]["erl2_vetoes"] > 0 for row in valid)
            summary["final_est_db"] = quantiles([row["trace"]["erl2_final_est_db"] for row in rows])
        if arm in ("F", "F2", "G", "G2", "H", "O", "Q", "R", "S", "T"):
            summary = result["arms"][arm]
            summary["double_talk_latency_ms"] = percentiles(
                [row["latency_ms"] for row in valid if row.get("echo_path", {}).get("double_talk")])
            summary["valid_trials_with_veto"] = sum(row["trace"]["nlms_vetoes"] > 0 for row in valid)
            summary["erle_db"] = quantiles(pooled)  # pooled over pre-onset render-active packets
            summary["erle_trial_p50_db"] = quantiles([row["trace"]["nlms_erle_db"] for row in rows
                                                      if row["trace"]["nlms_erle_db"] is not None])
            act = sum(row["trace"]["nlms_active_packets"] for row in rows)
            dtp = sum(row["trace"]["nlms_dt_packets"] for row in rows)
            summary["dt_packet_fraction"] = round(dtp / act, 4) if act else None
            summary["delay_error_ms"] = quantiles([row["trace"]["nlms_delay_ms"] - row["trace"]["nlms_delay_true_ms"]
                                                   for row in rows if row["trace"]["nlms_delay_ms"] is not None])
            if arm == "H":
                summary["copies_per_trial"] = quantiles([row["trace"]["nlms_copies"] for row in rows])
            if arm in ("O", "Q", "R", "S", "T"):
                summary["swaps_per_trial"] = quantiles([row["trace"]["nlms_swaps"] for row in rows])
                summary["trials_with_swap"] = sum(row["trace"]["nlms_swaps"] > 0 for row in rows)
        if arm in ("Q", "R", "S", "T"):  # bargein_replay/14 (/15-/17): latency_ms above is time-to-ducked-or-silent
            summary = result["arms"][arm]
            summary["latency_definition"] = "time_to_ducked_or_silent"
            summary["commit_latency_ms"] = percentiles([row["commit_latency_ms"] for row in valid
                                                        if row["commit_latency_ms"] is not None])
            summary["duck_false_alarms"] = sum(row["duck_false_alarms"] for row in rows)
            summary["duck_false_alarms_by_kind"] = {
                kind: sum(row["duck_false_alarms"] for row in rows if row["kind"] == kind)
                for kind in ("valid", "backchannel", "echo", "noise")}
            summary["ducks"] = sum(row["ducks"] for row in rows)
            summary["duck_ms_undone"] = quantiles([row["duck_ms_total"] for row in rows if row["duck_ms_total"]])
            summary["duck_ms_undone_total"] = sum(row["duck_ms_total"] for row in rows)
            summary["ducks_open_at_end"] = sum(row["duck_open_at_end"] for row in rows)
            summary["premature_ducks_valid"] = sum(
                bool(row["trace"].get("duck_ms")) and row["trace"]["duck_ms"][0]
                < row["trace"]["adapter_onset_frame_receive_ms"] for row in valid)
        if arm in ("S", "T"):  # bargein_replay/16 (/17): packets counted as speech by the pre-arm rule only
            result["arms"][arm]["prearm_speech_packets"] = prearm_packets
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
    parser.add_argument("--arm-e2", action="store_true",
                        help="add arm E2 (bargein_replay/8): energy gate with a least-squares matched-lag echo-gain fit")
    parser.add_argument("--erl2-margin-db", type=float, default=6.0,
                        help="arm E2: veto a hot packet when near <= 10^(margin/10) x fitted echo energy")
    parser.add_argument("--erl2-prior-db", type=float, default=-12.0, help="arm E2: echo-gain prior before the fit")
    parser.add_argument("--erl2-min-updates", type=int, default=10, help="arm E2: fitted packets before using the fit")
    parser.add_argument("--erl2-min-speech-ms", type=int, default=30,
                        help="arm E2: consecutive unvetoed hot speech before flush")
    parser.add_argument("--arm-f", action="store_true",
                        help="add arm F (bargein_replay/9): NLMS echo canceller, then arm D's gate on the residual")
    parser.add_argument("--arm-f2", action="store_true",
                        help="add arm F2 (bargein_replay/9): arm F vetoing against the canceller's echo estimate")
    parser.add_argument("--nlms-mu", type=float, default=0.5, help="arms F/F2: normalised NLMS step size")
    parser.add_argument("--nlms-delta", type=float, default=1000.0,
                        help="arms F/F2: regularisation per tap (delta = taps x this, int16 units squared)")
    parser.add_argument("--nlms-pre-ms", type=int, default=15, help="arms F/F2: filter span before the delay estimate")
    parser.add_argument("--nlms-post-ms", type=int, default=65, help="arms F/F2: filter span after the delay estimate")
    parser.add_argument("--nlms-geigel", type=float, default=0.5, help="arms F/F2: Geigel double-talk threshold")
    parser.add_argument("--nlms-hang-ms", type=int, default=30, help="arms F/F2: double-talk hangover")
    parser.add_argument("--nlms-min-updates", type=int, default=10,
                        help="arms F/F2: render-active packets before the delay estimate places the filter")
    parser.add_argument("--nlms-margin-db", type=float, default=6.0,
                        help="arm F2 (once converged): veto when the residual is >= this many dB below the capture")
    parser.add_argument("--nlms-converged-db", type=float, default=6.0,
                        help="arm F2: monitored ERLE (EWMA 0.1 over adapting packets) that switches from D's rule")
    parser.add_argument("--arm-g", action="store_true",
                        help="add arm G (bargein_replay/10): NLMS canceller, then a residual-only gate (no veto)")
    parser.add_argument("--arm-g2", action="store_true",
                        help="add arm G2 (bargein_replay/10): arm G with a residual echo suppressor (NLP)")
    parser.add_argument("--resid-threshold", type=float, default=480.0,
                        help="arms G/G2: speech when the 4 kHz residual RMS (G2: after the NLP) >= this")
    parser.add_argument("--resid-arm-db", type=float, default=6.0,
                        help="arms G/G2: armed (latched) once the filter is placed and its ERLE monitor >= this")
    parser.add_argument("--resid-nlp-db", type=float, default=3.0,
                        help="arm G2: over-subtraction of the estimated residual echo energy, dB")
    parser.add_argument("--arm-h", action="store_true",
                        help="add arm H (bargein_replay/11): arm G's gate after a two-path (copy-on-better) NLMS canceller")
    parser.add_argument("--twopath-copy-db", type=float, default=1.0,
                        help="arm H: copy background to foreground when its residual is >= this many dB lower")
    parser.add_argument("--twopath-copy-packets", type=int, default=2,
                        help="arm H: consecutive better packets before a copy")
    parser.add_argument("--arm-o", action="store_true",
                        help="add arm O (bargein_replay/12): arm G plus a Geigel-free shadow filter used only as a "
                             "stall/divergence guard")
    parser.add_argument("--guard-erle-db", type=float, default=6.0,
                        help="arm O: G's filter counts as stalled while its monitored ERLE is below this (dB)")
    parser.add_argument("--guard-margin-db", type=float, default=3.0,
                        help="arm O: the shadow residual must be at least this many dB below G's residual")
    parser.add_argument("--guard-packets", type=int, default=10,
                        help="arm O: consecutive stalled-and-shadow-better packets before the shadow replaces G's filter")
    parser.add_argument("--arm-q", action="store_true",
                        help="add arm Q (bargein_replay/14, needs --arm-o): arm O whose onset only ducks (pauses) "
                             "playout; the flush commits after --duck-commit-ms of cumulative residual speech within "
                             "--duck-window-ms, else playout resumes")
    parser.add_argument("--duck-commit-ms", type=int, default=100,
                        help="arm Q: cumulative residual-speech ms (onset run included) that commits the flush")
    parser.add_argument("--duck-window-ms", type=int, default=200,
                        help="arm Q: ms after the duck within which the commit must happen, else un-duck")
    parser.add_argument("--arm-r", action="store_true",
                        help="add arm R (bargein_replay/15, needs --arm-q): arm Q whose onset/duck run uses residual "
                             "RMS >= --split-duck-rms; the commit still counts residual RMS >= --resid-threshold")
    parser.add_argument("--split-duck-rms", type=float, default=None,
                        help="arm R (required with --arm-r): residual RMS threshold of the onset/duck run "
                             "(<= --resid-threshold)")
    parser.add_argument("--arm-s", action="store_true",
                        help="add arm S (bargein_replay/16, needs --arm-q): arm Q plus a pre-arm rule: while the "
                             "residual gate is not yet armed, residual RMS >= --prearm-rms also counts as speech")
    parser.add_argument("--prearm-rms", type=float, default=None,
                        help="arm S (required with --arm-s): pre-arm residual RMS threshold (>= --resid-threshold)")
    parser.add_argument("--arm-t", action="store_true",
                        help="add arm T (bargein_replay/17, needs --arm-s): arm S whose pre-arm rule also needs the "
                             "residual >= --prearm-echo-db above the predicted residual echo (render level x a "
                             "learned echo-return estimate)")
    parser.add_argument("--prearm-echo-db", type=float, default=None,
                        help="arm T (required with --arm-t): pre-arm margin over the predicted residual echo, dB")
    parser.add_argument("--echo-erle-db", type=float, default=None,
                        help="bargein_replay/8 (needs --echo-continuous): linear-AEC residual, echo attenuated X dB")
    parser.add_argument("--echo-model", choices=("copy", "residual"), default="copy",
                        help="residual: bargein_replay/2 filtered, attenuated, clipped, noisy echo plus double-talk")
    parser.add_argument("--echo-continuous", action="store_true",
                        help="bargein_replay/7 (needs --echo-model residual): residual echo in every capture while "
                        "the bot plays, not only in the event window")
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

    if cfg.echo_continuous and cfg.echo_model != "residual":
        parser.error("--echo-continuous needs --echo-model residual")

    if cfg.echo_erle_db is not None and (not cfg.echo_continuous or not 0 <= cfg.echo_erle_db <= 60):
        parser.error("--echo-erle-db needs --echo-continuous and 0 <= X <= 60 dB")
    if (cfg.erl2_min_speech_ms <= 0 or cfg.erl2_min_speech_ms % CAPTURE_MS or cfg.erl2_min_updates < 2
            or not -60 <= cfg.erl2_margin_db <= 60 or not -100 <= cfg.erl2_prior_db <= 40):
        parser.error("erl2 minimum speech must be a positive multiple of 10 ms; min updates >= 2; "
                     "margin in [-60, 60] dB; prior in [-100, 40] dB")

    if (not 0 < cfg.nlms_mu < 2 or cfg.nlms_delta <= 0 or not 0 <= cfg.nlms_pre_ms <= 100
            or not 10 <= cfg.nlms_post_ms <= 200 or not 0 < cfg.nlms_geigel <= 1 or cfg.nlms_hang_ms < 0
            or cfg.nlms_min_updates < 1):
        parser.error("nlms: mu in (0, 2), delta > 0, pre 0..100 ms, post 10..200 ms, geigel in (0, 1], "
                     "hang >= 0, min updates >= 1")

    if cfg.resid_threshold <= 0 or not -100 <= cfg.resid_arm_db <= 60 or not -60 <= cfg.resid_nlp_db <= 60:
        parser.error("resid: threshold > 0, arm dB in [-100, 60], NLP dB in [-60, 60]")

    if not -20 <= cfg.twopath_copy_db <= 60 or cfg.twopath_copy_packets < 1:
        parser.error("twopath: copy dB in [-20, 60], copy packets >= 1")
    if not -20 <= cfg.guard_erle_db <= 60 or not 0 <= cfg.guard_margin_db <= 60 or cfg.guard_packets < 1:
        parser.error("guard: ERLE dB in [-20, 60], margin dB in [0, 60], packets >= 1")
    for v in (cfg.duck_commit_ms, cfg.duck_window_ms):
        if v <= 0 or v % CAPTURE_MS or v > 2000:
            parser.error("duck: commit and window ms are positive multiples of 10 ms, <= 2000")
    if cfg.arm_q and not cfg.arm_o:
        parser.error("--arm-q needs --arm-o (arm Q is arm O with a duck/commit split)")
    if cfg.arm_r and not cfg.arm_q:
        parser.error("--arm-r needs --arm-q (arm R is arm Q with a separate duck threshold)")
    if cfg.arm_r and (cfg.split_duck_rms is None or not 0 < cfg.split_duck_rms <= cfg.resid_threshold):
        parser.error("--split-duck-rms must be in (0, --resid-threshold]")
    if cfg.arm_s and not cfg.arm_q:
        parser.error("--arm-s needs --arm-q (arm S is arm Q with a pre-arm speech rule)")
    if cfg.arm_s and (cfg.prearm_rms is None or not cfg.prearm_rms >= cfg.resid_threshold):
        parser.error("--prearm-rms is required with --arm-s and must be >= --resid-threshold")
    if cfg.arm_t and not cfg.arm_s:
        parser.error("--arm-t needs --arm-s (arm T is arm S with an echo-aware pre-arm rule)")
    if cfg.arm_t and (cfg.prearm_echo_db is None or not -200 <= cfg.prearm_echo_db <= 40):
        parser.error("--prearm-echo-db is required with --arm-t and must be in [-200, 40] dB")

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

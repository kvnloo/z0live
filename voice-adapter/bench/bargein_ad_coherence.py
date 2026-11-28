"""A-bargein-AD: per-packet residual vs echo-estimate coherence cue for arm U (bargein_replay/24, read-only import).

Zero-arm dev diagnostic (H_A36). No harness decision changes: arm U's gate class is replaced by a subclass whose
observe() re-states the inherited chain line for line and only records the canceller residual e, the NLMS echo
estimate y_hat (both at 4 kHz) and per-packet state. The canonical result sha256 that main() prints is kept in the
meta file so a run can be checked against the plain CLI (same flags, same seed).

Usage: python -B bargein_ad_coherence.py SEED COND(s1|s0) OUTDIR [TRIALS]
Writes OUTDIR/<cond>-<seed>.tsv (one row per packet of the sets P / N / N2) and OUTDIR/<cond>-<seed>.meta.json.

Cue (per 10 ms uplink packet; causal window of W = 80 samples at 4 kHz = 20 ms ending at the packet end):
  rho_L = |sum e[n] y[n+L]| / sqrt(sum e[n]^2 * sum y[n+L]^2), both indices inside the window (overlap trimmed);
  rho = max over L in -8..+8 (4 kHz samples); rho0 = rho_L at L = 0; mecc = rho with the 4 kHz capture d in place of e.
  Undefined (written "nan") when the canceller is not placed for the whole window or a norm is zero.
"""
import contextlib
import io
import json
import sys
from math import isfinite, sqrt
from operator import mul
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bargein_replay as br  # noqa: E402

BASE_ARGS = ("--echo-model residual --echo-continuous --bot-render recorded --clip-set multi --energy-k-db=-12.0 "
             "--energy-min-speech-ms 30 --nlms-mu 0.5 --nlms-delta 1000.0 --nlms-pre-ms 15 --nlms-post-ms 65 "
             "--nlms-geigel 0.5 --nlms-hang-ms 30 --nlms-min-updates 10 --nlms-margin-db=6.0 --nlms-converged-db=6.0 "
             "--resid-threshold 1360.0 --resid-arm-db=6.0 --resid-nlp-db=3.0 --arm-o --guard-erle-db=6.0 "
             "--guard-margin-db=3.0 --guard-packets 5 --arm-q --duck-commit-ms 100 --duck-window-ms 300 --arm-s "
             "--prearm-rms 5000.0 --arm-u").split()
W = 80        # 20 ms at 4 kHz
LAGS = 8      # +-8 samples at 4 kHz
P_MAX = 3     # first 3 onset packets per slow-or-missed S1 trial
N2_MAX = 5    # descriptive sub-threshold echo-only packets per S0 trial

SM = br.SAMPLES_MS
CAP = br.CAPTURE_MS
CUR = {"gate": None}
COND = None


class Inst(br.NoDuckPreArmGate):
    """Arm U with bookkeeping only: observe() re-states NoDuckPreArmGate.observe -> DuckConfirmGate.observe ->
    ShadowGuardGate.observe line for line, storing the residual e and the echo estimate y_hat of every 4 kHz sample
    and per-packet state."""

    def __init__(self, cfg, span_ms):
        super().__init__(cfg, span_ms)
        self.resbuf = [0.0] * (span_ms * 4 + 16)
        self.ybuf = [0.0] * (span_ms * 4 + 16)
        self.pk = []
        self.placed_at = None
        CUR["gate"] = self

    def observe(self, t, packet):
        rec = dict(t=t, ducked0=self.ducked, silent0=self.silent, fired0=self.fired)
        # DuckConfirmGate.observe
        if self.fired:
            event = None
        else:
            self.speech = False
            self._shadow_observe(t, packet, rec)
            event = self.step(t, self.speech)
        # NoDuckPreArmGate.observe
        if self.armed and self.armed_ms is None:
            self.armed_ms = t
        rec.update(event=event, ducked1=self.ducked, silent1=self.silent, fired1=self.fired)
        self.pk.append(rec)
        return event

    def _shadow_observe(self, t, packet, rec):  # ShadowGuardGate.observe, unchanged arithmetic + buffers / rec
        if self.fired:
            return False
        end = t * br.SAMPLES_MS
        self.near[end - len(packet):end] = packet
        self.advance(end)
        m1 = end // 4
        m0 = m1 - len(packet) // 4
        n = m1 - m0
        r4, d4, p4 = self.r4, self.d4, self.p4
        lag4 = self.lag_ms * 4
        top = max((p4[e] - p4[e - n] for e in range(max(n, m1 - lag4), m1 + 1)), default=0.0)
        active = top >= n * br.THRESHOLD ** 2
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
        if placed and self.placed_at is None:
            self.placed_at = m0
        rb, yb = self.resbuf, self.ybuf
        if not placed:
            for m in range(m0, m1):
                ee += d4[m] * d4[m]
                rb[m] = d4[m]
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
                rb[m] = e
                yb[m] = y
                ee += e * e
                ey += y * y
                norm = p4[max(0, hi)] - p4[max(0, lo)]
                if adapt:
                    if norm:
                        g = mu * e / (norm + delta)
                        self.w = [a + g * b for a, b in zip(w, x)]
                        self.updates += 1
                ws = self.ws
                b = d4[m] - sum(map(mul, ws, x))
                es += b * b
                if active and norm:
                    g = mu * b / (norm + delta)
                    self.ws = [a + g * v for a, v in zip(ws, x)]
        if adapt:
            q1 = end // br.SAMPLES_MS
            self.estimate(q1 - len(packet) // br.SAMPLES_MS, q1)
        ed = sum(d4[m] * d4[m] for m in range(m0, m1))
        if active:
            self.log.append((t, ed, ee))
        if adapt and self.delay_ms is not None and ed and ee:
            self.erle_hat_db = 0.9 * self.erle_hat_db + 0.1 * 10 * br.log10(ed / ee)
        speech = self.decide(n, ee, ed, ey, top)
        rec.update(m1=m1, end=end, rrms=sqrt(ee / n), crms=sqrt(ed / n), speech=bool(self.speech), armed=self.armed,
                   active=bool(active), placed=placed)
        if active and placed:
            if ed and es:
                self.shadow_erle_db = 0.9 * self.shadow_erle_db + 0.1 * 10 * br.log10(ed / es)
            stalled = self.erle_hat_db < self.guard_erle_db
            self.guard_run = self.guard_run + 1 if stalled and es <= self.guard_ratio * ee else 0
            if self.guard_run >= self.guard_packets:
                self.w = list(self.ws)
                self.erle_hat_db = self.shadow_erle_db
                self.swaps += 1
                self.guard_run = 0
        self.run = self.run + br.CAPTURE_MS if speech else 0
        if self.run >= self.minimum:
            self.fired = True
            return True
        return False


# ---- cue ------------------------------------------------------------------------------------------------------
def coh(a, b):
    """(max over lags -LAGS..LAGS, lag-0 value) of |<a[n], b[n+L]>| / (||a|| ||b||) over the overlap; None if
    undefined."""
    n = len(a)
    best = zero = None
    for lag in range(-LAGS, LAGS + 1):
        if lag >= 0:
            x, y = a[:n - lag], b[lag:]
        else:
            x, y = a[-lag:], b[:n + lag]
        den = sum(map(mul, x, x)) * sum(map(mul, y, y))
        if den <= 0:
            continue
        r = abs(sum(map(mul, x, y))) / sqrt(den)
        if best is None or r > best:
            best = r
        if lag == 0:
            zero = r
    return best, zero


def cues(gate, rec):
    m1 = rec["m1"]
    lo = m1 - W
    if lo < 0 or gate.placed_at is None or lo < gate.placed_at:
        return None, None, None
    e = gate.resbuf[lo:m1]
    y = gate.ybuf[lo:m1]
    d = [float(v) for v in gate.d4[lo:m1]]
    rho, rho0 = coh(e, y)
    mecc, _ = coh(d, y)
    return rho, rho0, mecc


def near_rms(trial, rec):
    """RMS of the near-end source component (caller clip, backchannel or noise burst; gain-scaled) inside the
    packet's capture window. 0.0 = the packet holds only echo residual and the noise floor. Label only."""
    mic = trial["mic"]
    if mic is None:
        return 0.0
    tcap = rec["t"] - trial["up"]  # capture frame end (phone clock)
    origin = trial["onset"] * SM
    acc = 0.0
    for i in range((tcap - CAP) * SM, tcap * SM):
        o = i - origin
        if 0 <= o < len(mic):
            acc += mic[o] * mic[o]
    return sqrt(acc / (CAP * SM))


def windows(pk):
    """Reconstruct U's onset windows from packet state: list of dict(open_t, packets, paused, end)."""
    out, cur = [], None
    for i, r in enumerate(pk):
        if r["fired0"] or "speech" not in r:
            continue
        if cur is None:
            if r["ducked1"] or r["event"] == "commit":
                j = i  # the consecutive speech run ending here (onset run)
                while j - 1 >= 0 and pk[j - 1].get("speech") and not pk[j - 1]["ducked0"]:
                    j -= 1
                cur = dict(open_t=r["t"], packets=list(range(j, i + 1)), paused=r["event"] == "duck",
                           silent=r["silent1"], end=None)
                if r["event"] == "commit":
                    cur["end"] = "commit"
                    out.append(cur)
                    cur = None
            continue
        if r["speech"]:
            cur["packets"].append(i)
        if r["event"] == "duck":
            cur["paused"] = True
        if r["event"] == "commit":
            cur["end"] = "commit"
        elif r["event"] == "unduck":
            cur["end"] = "unduck"
        elif not r["ducked1"]:
            cur["end"] = "lapse"
        if cur["end"]:
            out.append(cur)
            cur = None
    if cur is not None:
        cur["end"] = "open"
        out.append(cur)
    return out


ROWS = []
COUNTS = dict(trials=0, slow=0, false_cut_windows=0, duck_fa_windows=0, lapse_windows=0, premature_commit=0)
_orig_replay = br.replay


def fmt(v):
    return "nan" if v is None or not isfinite(v) else f"{v:.6f}"


def replay(trial, arm, cfg, bot):
    CUR["gate"] = None
    row = _orig_replay(trial, arm, cfg, bot)
    gate = CUR["gate"]
    if arm != "U" or gate is None:
        return row
    COUNTS["trials"] += 1
    pk, kind, tid = gate.pk, trial["kind"], trial["id"]
    onset_rx = row["trace"].get("adapter_onset_frame_receive_ms")
    wins = windows(pk)
    assert sum(w["paused"] for w in wins) == row["ducks"], (tid, wins, row["ducks"])  # harness's own duck count
    emit = []
    if COND == "s1" and kind == "valid" and (row["missed_valid"] or row["latency_ms"] > 150):
        COUNTS["slow"] += 1
        first_win = min((w["open_t"] for w in wins if w["open_t"] >= onset_rx), default=None)
        k = 0
        for r in pk:
            if k >= P_MAX or "speech" not in r:
                continue
            t = r["t"]
            if not onset_rx <= t < onset_rx + 200 or (first_win is not None and t >= first_win):
                continue
            if r["armed"] and not r["speech"] and near_rms(trial, r) >= 120:
                emit.append(("P", "slow", r))
                k += 1
    if COND == "s0":
        for w in wins:
            premature = kind == "valid" and w["open_t"] < onset_rx
            if kind != "valid" and w["end"] == "commit":
                tag = "fc"
                COUNTS["false_cut_windows"] += 1
            elif (kind != "valid" or premature) and w["paused"] and w["end"] != "commit":
                tag = "duckfa"
                COUNTS["duck_fa_windows"] += 1
            else:
                if w["end"] == "lapse" and (kind != "valid" or premature):
                    COUNTS["lapse_windows"] += 1
                if premature and w["end"] == "commit":
                    COUNTS["premature_commit"] += 1
                continue
            for i in w["packets"]:
                if pk[i].get("speech"):
                    emit.append(("N", tag, pk[i]))
        k = 0
        for r in pk:
            if k >= N2_MAX or "speech" not in r or r["fired0"]:
                continue
            if kind == "valid" and r["t"] >= onset_rx:
                continue
            if r["armed"] and not r["speech"] and 300 <= r["rrms"] < 1360 and near_rms(trial, r) == 0.0:
                emit.append(("N2", "sub", r))
                k += 1
    for s, tag, r in emit:
        rho, rho0, mecc = cues(gate, r)
        ROWS.append((s, kind, tag, tid, r["t"], int(r["armed"]), round(r["rrms"], 1), round(r["crms"], 1),
                     round(near_rms(trial, r), 1), fmt(rho), fmt(rho0), fmt(mecc), row.get("caller_gain_db", "")))
    gate.pk = gate.resbuf = gate.ybuf = None  # free memory
    return row


def main():
    global COND
    seed, COND, outdir = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    trials = int(sys.argv[4]) if len(sys.argv) > 4 else 200
    assert COND in ("s1", "s0"), COND
    br.replay = replay
    br.NoDuckPreArmGate = Inst
    args = BASE_ARGS + ["--only-arms", "U", "--seed", str(seed), "--trials", str(trials)]
    if COND == "s1":
        args.append("--caller-level-db-range=-10,0")
    sys.argv = ["bargein_replay.py"] + args
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        br.main()
    summary = json.loads(buf.getvalue())
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / f"{COND}-{seed}.tsv", "w") as f:
        f.write("set\tkind\ttag\ttrial\tt\tarmed\tres_rms\tcap_rms\tnear_rms\trho\trho0\tmecc\tcaller_gain_db\n")
        for row in ROWS:
            f.write("\t".join(map(str, row)) + "\n")
    u = summary["arms"]["U"]
    meta = dict(seed=seed, cond=COND, trials=trials, sha256=summary["sha256"], harness=summary.get("harness"),
                counts=COUNTS, U=dict(valid_over_150_or_missed=u["valid_over_150_or_missed"], missed=u["missed_valid"],
                                      false_barge_ins=u["false_barge_ins"], duck_false_alarms=u["duck_false_alarms"]))
    (outdir / f"{COND}-{seed}.meta.json").write_text(json.dumps(meta, sort_keys=True) + "\n")
    print(json.dumps(meta, sort_keys=True))


if __name__ == "__main__":
    main()

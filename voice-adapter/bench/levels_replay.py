"""Offline scorer for opt-in `audio.levels` call traces (Z0LIVE_TRACE_LEVELS=1): integrity, echo floor, barge-in.

Input: one call trace JSONL (call_trace.Trace lines: {"t", "wall", "ev", ...}) as a path argument or on stdin. Only
`audio.levels` events are read; each carries one direction's frames for a ~250 ms window:
  dir   "input" (phone mic -> adapter, the uplink) or "output" (voice -> phone, the downlink)
  t     ms since the call trace t0, one per frame       rms, peak  dBFS per frame (1 decimal, floor -120.0)
  n     samples per frame: an int when all equal, else a list (frame duration = n / --rate)

Integrity (per direction): frames, windows (events), duplicate t, non-monotonic t (a t below its predecessor),
gaps (consecutive frames more than 2 x the frame duration apart) and total gap ms (the time beyond one frame).
Bad lines (not JSON / not an object) and bad audio.levels events (missing dir, length mismatch, non-numeric) are
counted, never fatal. Metrics use each direction's frames sorted by t with duplicate t dropped.

Metrics (energy only, like the adapter's Level; no speech recognition):
  downlink spans  Level's on/off rule replayed: a frame is loud when peak >= --on-db (default -32.2 dBFS =
                  mobile_live.LEVEL_ON 800 on the trace's 1-decimal scale); a span starts at a loud frame and
                  ends at the first quiet frame once quiet has lasted --hangover-ms (LEVEL_OFF_SECONDS, 300).
                  Frames until the off decision (hangover included) are downlink-active.
  uplink levels   p50/p95 uplink RMS over frames while the downlink is active vs silent.
  echo floor      p95 uplink RMS over uplink frames whose nearest downlink frame is loud (hangover excluded:
                  that is where echo of the voice can be). Barge-ins inside that set lift it only above 5 %.
                  --floor-mode picks the estimator over that set (default p95): p50 = median; min = median
                  across downlink spans of the per-span 10th percentile; early = p95 of the frames in the first
                  200 ms of their span (echo before a caller can react), p50 of the set if under 5 such frames.
  double talk     uplink frames, downlink active, with RMS > echo floor + --margin-db.
  barge-in        candidate onsets: the first frame of each double-talk run lasting >= --min-run-ms (a run breaks
                  on a non-double-talk frame or a gap), reported with the containing downlink span's start.
                  --onset-mode hindsight (offline only, non-causal) replaces these: per closed span, if the caller
                  is still talking in the 400 ms after the span's off decision (>= min-run of frames above the
                  span's first-200 ms uplink p50 + --hindsight-db), trace back through the span to the earliest
                  such frame, bridging pauses up to 300 ms.

Output: a human summary, or --json (schema levels_replay/1, sorted keys, deterministic). stdlib + numpy only.

  python3 bench/levels_replay.py /path/to/call.jsonl --json
  python3 bench/levels_replay.py --margin-db 8 --min-run-ms 160 < call.jsonl
"""
import argparse
from dataclasses import asdict, dataclass
import json
import math
import sys

import numpy as np

SCHEMA = "levels_replay/1"
LEVELS_FLOOR_DB = -120.0  # mobile_live.LEVELS_FLOOR_DB
LEVEL_ON = 800            # mobile_live.LEVEL_ON (s16 peak)
LEVEL_OFF_MS = 300        # mobile_live.LEVEL_OFF_SECONDS
UP, DOWN = "input", "output"


@dataclass(frozen=True)
class Params:
    on_db: float = -32.2        # round(20 log10(LEVEL_ON / 32768), 1)
    hangover_ms: float = LEVEL_OFF_MS
    margin_db: float = 6.0
    min_run_ms: float = 200.0
    rate: int = 48000
    up: str = UP
    down: str = DOWN
    floor_mode: str = "p95"     # echo-floor estimator, one of FLOOR_MODES
    onset_mode: str = "run"     # barge-in onset rule, one of ONSET_MODES
    hindsight_db: float = 9.0   # onset_mode hindsight: dB above the span's early uplink p50 that counts as hot


FLOOR_MODES = ("p95", "p50", "min", "early")
EARLY_MS = 200.0    # floor_mode early: echo frames this close to their span start (before a caller can react)
EARLY_MIN_FRAMES = 5
ONSET_MODES = ("run", "hindsight")
POST_MS = 400.0     # onset_mode hindsight: window after a span's off decision that shows the caller still talking
GAP_MS = 300.0      # onset_mode hindsight: a non-hot stretch longer than this ends the back-trace


def hindsight_onsets(ut, urms, u_span, u_active, spans, up_ms, p):
    """Non-causal (offline only) onsets: per closed downlink span k, reference r = p50 uplink RMS of the span's first
    EARLY_MS; hot = RMS > r + hindsight_db. If >= ceil(min_run_ms / up_ms) uplink frames in [active_until,
    active_until + POST_MS) (downlink inactive) are hot, walk the span's uplink frames back from the last one and keep
    the earliest hot frame reached before a non-hot stretch (or time gap) longer than GAP_MS. No hot frame -> none."""
    out = []
    need = math.ceil(p.min_run_ms / up_ms)
    for k, (s0, _end, until, is_open) in enumerate(spans):
        if is_open:
            continue
        idx = np.flatnonzero(u_span == k)
        if not idx.size:
            continue
        early = urms[idx[(ut[idx] - s0) < EARLY_MS]]
        if early.size < EARLY_MIN_FRAMES:
            continue
        hot_db = float(np.percentile(early, 50)) + p.hindsight_db
        post = (ut >= until) & (ut < until + POST_MS) & ~u_active
        if int(np.count_nonzero(urms[post] > hot_db)) < need:
            continue
        hot = idx[urms[idx] > hot_db]
        if not hot.size:
            continue
        first = last = hot[-1]
        for h in hot[-2::-1]:
            if ut[first] - ut[h] - up_ms > GAP_MS:
                break
            first = h
        s0 = int(s0)
        out.append({"t_ms": int(ut[first]), "span_start_ms": s0, "rel_ms": int(ut[first]) - s0,
                    "run_ms": int(ut[last] - ut[first] + up_ms),
                    "max_rms_db": round(float(urms[first:last + 1].max()), 1)})
    return out


def echo_floor(echo, rel_ms, span, mode):
    """Echo floor dBFS (unrounded) over the echo set: uplink RMS `echo`, ms since the containing downlink span
    start `rel_ms`, span index `span`. None when the set is empty.
      p95    p95 of the set (double talk lifts it once it exceeds 5 % of the set)
      p50    median of the set
      min    per span, the 10th percentile; then the median across spans
      early  p95 of the frames < EARLY_MS into their span; p50 of the set if fewer than EARLY_MIN_FRAMES"""
    if mode not in FLOOR_MODES:
        raise ValueError(f"floor_mode {mode!r} not in {FLOOR_MODES}")
    if not echo.size:
        return None
    if mode == "p95":
        return float(np.percentile(echo, 95))
    if mode == "p50":
        return float(np.percentile(echo, 50))
    if mode == "min":
        per = [float(np.percentile(echo[span == k], 10)) for k in np.unique(span[span >= 0])]
        return float(np.median(per)) if per else None
    early = echo[(span >= 0) & (rel_ms < EARLY_MS)]
    return float(np.percentile(early if early.size >= EARLY_MIN_FRAMES else echo,
                               95 if early.size >= EARLY_MIN_FRAMES else 50))


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class Direction:
    """One direction's frames in arrival (trace) order."""
    def __init__(self):
        self.windows = 0
        self._t, self._rms, self._peak, self._n = [], [], [], []
        self._arrays = None

    def add(self, t, rms, peak, n):
        self.windows += 1
        self._t += t
        self._rms += rms
        self._peak += peak
        self._n += n
        self._arrays = None

    def _build(self):
        if self._arrays is None:
            self._arrays = (np.asarray(self._t, dtype=np.int64), np.asarray(self._rms, dtype=np.float64),
                            np.asarray(self._peak, dtype=np.float64), np.asarray(self._n, dtype=np.int64))
        return self._arrays

    t = property(lambda self: self._build()[0])
    rms = property(lambda self: self._build()[1])
    peak = property(lambda self: self._build()[2])
    n = property(lambda self: self._build()[3])

    def clean(self):
        """(t, rms, peak, n) sorted by t (stable) with duplicate t dropped (first arrival kept)."""
        t, rms, peak, n = self._build()
        if not t.size:
            return t, rms, peak, n
        order = np.argsort(t, kind="stable")
        t = t[order]
        keep = np.ones(t.size, dtype=bool)
        keep[1:] = np.diff(t) != 0
        return t[keep], rms[order][keep], peak[order][keep], n[order][keep]


class Trace:
    def __init__(self):
        self.dirs = {}
        self.malformed_lines = 0
        self.malformed_events = 0
        self.lines = 0

    def direction(self, name):
        return self.dirs.get(name) or Direction()


def _event(ev):
    """(dir, t, rms, peak, n-list) of a valid audio.levels event, else None."""
    d, t, rms, peak, n = ev.get("dir"), ev.get("t"), ev.get("rms"), ev.get("peak"), ev.get("n")
    if not isinstance(d, str) or not all(isinstance(x, list) for x in (t, rms, peak)):
        return None
    k = len(t)
    if not k or len(rms) != k or len(peak) != k:
        return None
    if not all(_num(x) for x in t) or not all(_num(x) for x in rms) or not all(_num(x) for x in peak):
        return None
    if isinstance(n, list):
        if len(n) != k:
            return None
        ns = n
    else:
        ns = [n] * k
    if not all(_num(x) and x > 0 and x == int(x) for x in ns):
        return None
    return d, [int(round(x)) for x in t], [float(x) for x in rms], [float(x) for x in peak], [int(x) for x in ns]


def parse(lines):
    """Collect audio.levels per direction from trace lines (an iterable of str). Never raises on content."""
    tr = Trace()
    for raw in lines:
        if not raw.strip():
            continue
        tr.lines += 1
        try:
            ev = json.loads(raw)
        except ValueError:
            tr.malformed_lines += 1
            continue
        if not isinstance(ev, dict):
            tr.malformed_lines += 1
            continue
        if ev.get("ev") != "audio.levels":
            continue
        got = _event(ev)
        if got is None:
            tr.malformed_events += 1
            continue
        d, *frames = got
        tr.dirs.setdefault(d, Direction()).add(*frames)
    return tr


def _frame_ms(n, rate):
    return float(np.median(n)) * 1000.0 / rate if n.size else 20.0


def integrity(d, rate):
    t = d.t
    frame_ms = _frame_ms(d.n, rate)
    out = {"frames": int(t.size), "windows": d.windows, "duplicate_t": 0, "non_monotonic_t": 0,
           "gaps": 0, "gap_ms": 0, "frame_ms": round(frame_ms, 3)}
    if t.size > 1:
        step = np.diff(t)
        out["duplicate_t"] = int(np.count_nonzero(step == 0))
        out["non_monotonic_t"] = int(np.count_nonzero(step < 0))
        ct = d.clean()[0]
        cstep = np.diff(ct)
        big = cstep > 2 * frame_ms
        out["gaps"] = int(np.count_nonzero(big))
        out["gap_ms"] = int(round(float(np.sum(cstep[big] - frame_ms))))
    return out


def downlink_spans(t, peak, frame_ms, p):
    """Replay Level.frame's on/off: [(start_ms, end_ms, active_until_ms, open)] and the loud mask."""
    loud = peak >= p.on_db
    spans, on, quiet = [], None, None
    for i in range(t.size):  # 30k frames/dir is ~10 ms in pure Python; exactness beats vectorising here
        now = int(t[i])
        if loud[i]:
            quiet = None
            if on is None:
                on = now
        elif on is not None:
            if quiet is None:
                quiet = now
            if now - quiet >= p.hangover_ms:
                spans.append((on, quiet, now, False))
                on = quiet = None
    if on is not None:
        last = int(t[-1]) + frame_ms
        spans.append((on, quiet if quiet is not None else last, last, True))
    return spans, loud


def _stats(x):
    if not x.size:
        return {"frames": 0, "p50": None, "p95": None}
    return {"frames": int(x.size), "p50": round(float(np.percentile(x, 50)), 1),
            "p95": round(float(np.percentile(x, 95)), 1)}


def score(tr, p=None):
    p = p or Params()
    if p.floor_mode not in FLOOR_MODES:
        raise ValueError(f"floor_mode {p.floor_mode!r} not in {FLOOR_MODES}")
    if p.onset_mode not in ONSET_MODES:
        raise ValueError(f"onset_mode {p.onset_mode!r} not in {ONSET_MODES}")
    names = sorted(set(tr.dirs) | {p.up, p.down})
    integ = {name: integrity(tr.direction(name), p.rate) for name in names}
    up, down = tr.direction(p.up), tr.direction(p.down)
    ut, urms, _, un = up.clean()
    dt, _, dpeak, dn = down.clean()
    up_ms, down_ms = _frame_ms(un, p.rate), _frame_ms(dn, p.rate)

    spans, dloud = downlink_spans(dt, dpeak, down_ms, p)
    starts = np.asarray([s[0] for s in spans], dtype=np.float64)
    until = np.asarray([s[2] for s in spans], dtype=np.float64)

    def active(times):
        if not starts.size or not times.size:
            return np.zeros(times.size, dtype=bool), np.full(times.size, -1)
        k = np.searchsorted(starts, times, side="right") - 1
        ok = (k >= 0) & (times < until[np.maximum(k, 0)])
        return ok, np.where(ok, k, -1)

    d_active = active(dt.astype(np.float64))[0]
    u_active, u_span = active(ut.astype(np.float64))
    # Loud (echo-capable) uplink frames: the latest downlink frame at or before t is loud and not stale.
    if dt.size and ut.size:
        j = np.searchsorted(dt, ut, side="right") - 1
        jj = np.maximum(j, 0)
        u_loud = (j >= 0) & dloud[jj] & (ut - dt[jj] < 2 * down_ms)
    else:
        u_loud = np.zeros(ut.size, dtype=bool)

    echo = urms[u_loud]
    e_span = u_span[u_loud]
    e_rel = ut[u_loud].astype(np.float64) - np.where(e_span >= 0, starts[np.maximum(e_span, 0)] if starts.size
                                                      else 0.0, np.inf)
    floor = echo_floor(echo, e_rel, e_span, p.floor_mode)
    floor = round(floor, 1) if floor is not None else None
    onsets, dt_frames, threshold = [], 0, None
    if floor is not None:
        threshold = round(floor + p.margin_db, 1)
        hot = u_active & (urms > threshold)
        dt_frames = int(np.count_nonzero(hot))
        if dt_frames:
            idx = np.flatnonzero(hot)
            brk = np.ones(idx.size, dtype=bool)
            brk[1:] = (np.diff(idx) != 1) | (np.diff(ut[idx]) > 2 * up_ms) | (np.diff(u_span[idx]) != 0)
            heads = np.flatnonzero(brk)
            tails = np.append(heads[1:], idx.size) - 1
            for h, e in zip(heads, tails):
                a, b = idx[h], idx[e]
                run_ms = int(ut[b] - ut[a] + up_ms)
                if run_ms >= p.min_run_ms:
                    s0 = int(starts[u_span[a]])
                    onsets.append({"t_ms": int(ut[a]), "span_start_ms": s0, "rel_ms": int(ut[a]) - s0,
                                   "run_ms": run_ms, "max_rms_db": round(float(urms[a:b + 1].max()), 1)})
    if p.onset_mode == "hindsight":  # replaces the run rule's onsets; floor, threshold and double talk unchanged
        onsets = hindsight_onsets(ut, urms, u_span, u_active, spans, up_ms, p)

    return {
        "schema": SCHEMA,
        "params": asdict(p),
        "integrity": {"lines": tr.lines, "malformed_lines": tr.malformed_lines,
                      "malformed_events": tr.malformed_events, "dirs": integ},
        "downlink": {
            "spans": [{"start_ms": s, "end_ms": int(e), "open": o} for s, e, _, o in spans],
            "active_frames": int(np.count_nonzero(d_active)),
            "loud_frames": int(np.count_nonzero(dloud)),
            "active_ms": int(round(sum(e - s for s, e, _, _ in spans))),
        },
        "uplink": {"down_active": _stats(urms[u_active]), "down_silent": _stats(urms[~u_active])},
        "echo": {"floor_db": floor, "floor_mode": p.floor_mode, "frames": int(echo.size),
                 "p50_db": round(float(np.percentile(echo, 50)), 1) if echo.size else None},
        "bargein": {"threshold_db": threshold, "double_talk_frames": dt_frames, "onsets": onsets},
    }


def summary(rep):
    g, out = rep["integrity"], [f"{rep['schema']}  lines={rep['integrity']['lines']}"
                                f"  malformed lines={rep['integrity']['malformed_lines']}"
                                f" events={rep['integrity']['malformed_events']}"]
    for name, d in sorted(g["dirs"].items()):
        out.append(f"  {name:7s} frames={d['frames']} windows={d['windows']} frame_ms={d['frame_ms']}"
                   f" dup_t={d['duplicate_t']} non_monotonic_t={d['non_monotonic_t']}"
                   f" gaps={d['gaps']} ({d['gap_ms']} ms)")
    dl, ul, e, b = rep["downlink"], rep["uplink"], rep["echo"], rep["bargein"]
    out.append(f"downlink spans: {len(dl['spans'])}  active {dl['active_ms']} ms, {dl['active_frames']} frames")
    for k in ("down_active", "down_silent"):
        s = ul[k]
        out.append(f"uplink rms while {k.replace('down_', 'downlink ')}: frames={s['frames']}"
                   f" p50={s['p50']} p95={s['p95']} dBFS")
    out.append(f"echo floor ({e['floor_mode']}, uplink rms, downlink loud): {e['floor_db']} dBFS  p50={e['p50_db']}"
               f"  frames={e['frames']}")
    out.append(f"double-talk frames: {b['double_talk_frames']}  threshold={b['threshold_db']} dBFS")
    out.append(f"barge-in onsets: {len(b['onsets'])}")
    for o in b["onsets"]:
        out.append(f"  t={o['t_ms']} ms  +{o['rel_ms']} ms into span@{o['span_start_ms']}"
                   f"  run={o['run_ms']} ms  max={o['max_rms_db']} dBFS")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("trace", nargs="?", default="-", help="call trace JSONL (default stdin)")
    ap.add_argument("--json", action="store_true", help=f"machine output ({SCHEMA})")
    d = Params()
    ap.add_argument("--on-db", type=float, default=d.on_db, help="downlink peak dBFS that counts as sound")
    ap.add_argument("--hangover-ms", type=float, default=d.hangover_ms, help="quiet this long ends a span")
    ap.add_argument("--margin-db", type=float, default=d.margin_db, help="uplink dB above echo floor = double talk")
    ap.add_argument("--min-run-ms", type=float, default=d.min_run_ms, help="double-talk run for a barge-in onset")
    ap.add_argument("--rate", type=int, default=d.rate, help="sample rate behind n (frame ms = n / rate)")
    ap.add_argument("--up-dir", default=d.up)
    ap.add_argument("--down-dir", default=d.down)
    ap.add_argument("--floor-mode", choices=FLOOR_MODES, default=d.floor_mode, help="echo-floor estimator")
    ap.add_argument("--onset-mode", choices=ONSET_MODES, default=d.onset_mode,
                    help="barge-in onset rule (hindsight: offline back-trace from a caller still talking after a span)")
    ap.add_argument("--hindsight-db", type=float, default=d.hindsight_db, help="hindsight hot dB over span-start p50")
    a = ap.parse_args(argv)
    p = Params(on_db=a.on_db, hangover_ms=a.hangover_ms, margin_db=a.margin_db, min_run_ms=a.min_run_ms,
               rate=a.rate, up=a.up_dir, down=a.down_dir, floor_mode=a.floor_mode,
               onset_mode=a.onset_mode, hindsight_db=a.hindsight_db)
    if a.trace == "-":
        tr = parse(sys.stdin)
    else:
        with open(a.trace, encoding="utf-8", errors="replace") as f:
            tr = parse(f)
    rep = score(tr, p)
    sys.stdout.write(json.dumps(rep, sort_keys=True, indent=1) + "\n" if a.json else summary(rep) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

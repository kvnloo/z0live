"""Offline floor-control policies on the Dessa reference calls, scored like the Track A bench (Track C).

Each policy replays a Dessa human track. Every 20 ms it may take the floor or backchannel, from the human audio heard
so far (floor_data features); its sound lands --delay s later. A take plays Dessa's own reply to the human talkspurt
it follows, shifted to the policy's onset; a take where Dessa did not answer plays GENERIC s of speech (a false
cut-in). A backchannel plays a CLIP s clip. The AI track that results is scored by dessa_score.per_call against the
human and Dessa tracks: D, false cut-in %, missed-take %, backchannels/min.

Policies (leave-one-call-out over the 20 calls; parameters picked on the other 19 by J = D + false cut-in % / 100):
  dessa     Dessa's own timing through the simulator; must reproduce dessa_timing on the raw tracks (identity check)
  oracle    takes at the true end of every human talkspurt Dessa answered, backchannels where Dessa made them
  rules     the deployed constants: take after LEVEL_OFF_SECONDS (0.3 s) of Level quiet; A10's backchannel trigger
  rules*    the same rule family with detector, take threshold and backchannel trigger tuned per fold
  gate      rules* plus an earlier take when a logistic pause classifier (turn end vs hold) is confident
  logistic  per-frame hazard heads (take, backchannel); thresholds and a fallback timeout tuned per fold
  gbm       the same heads as gradient-boosted trees
  NAME+text the same with transcript features (floor_asr.py must have run)

  python bench/floor_eval.py [--delay 0.25] [--policies rules,rules*,gate] [--no-bc]
    -> table on stdout, JSON under ~/.local/share/z0live/orch/ (--no-bc: take decisions alone, no backchannels)
"""
import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bc_sim  # noqa: E402
import dessa_score as ds  # noqa: E402
import floor_data as fd  # noqa: E402

OUT = Path.home() / ".local/share/z0live/orch"
CLIP = 0.4       # backchannel clip (bc_sim --clip-s; A10's "Mm-h")
GENERIC = 1.0    # speech a false cut-in plays before the human resumes (guess; no barge-in flush is modelled)
BLOCK = 10.0     # bootstrap block, s (dessa_score.BLOCK_S)
QI, LQI = fd.FEATURES.index("quiet_s"), fd.FEATURES.index("lquiet_s")


# ---------------------------------------------------------------- simulator

def merge(spurts):
    """dessa_timing.spurts' bridging on placed talkspurts (10 ms grid): pauses < 180 ms merge, bursts < 90 ms drop."""
    out = []
    for s, e in sorted((round(s, 2), round(e, 2)) for s, e in spurts):
        if out and round(s - out[-1][1], 2) < 0.18:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out if round(e - s, 2) >= 0.09]


class Sim:
    """The AI track one policy produces on one call."""

    def __init__(self, call, sc, delay):
        self.delay, self.replies, self.human = delay, {r.answers: r for r in sc.replies}, call.human
        self.starts = np.array([s for s, _ in call.human])
        self.placed = list(sc.opening)
        self.clips = set()  # backchannel onsets: a clip that lands after the human stopped scores as a floor-take

    def take(self, t):
        """Decided at t: Dessa's reply to the human talkspurt before the onset, else a false cut-in. -> end time."""
        tau = round(t + self.delay, 2)
        j = int(np.searchsorted(self.starts, tau, side="right")) - 1
        if j >= 0 and self.starts[j] == tau:
            # The human starts in the same 10 ms: the AI answers what it heard (j - 1) if that is also how
            # dessa_timing will order the two (by (start, end): the AI's first talkspurt must end first).
            r = self.replies.get(j - 1)
            if tau + (r.spurts[0][1] - r.onset if r else GENERIC) <= self.human[j][1]:
                j -= 1
        r = self.replies.pop(j, None)
        new = [(s - r.onset + tau, e - r.onset + tau) for s, e in r.spurts] if r else [(tau, tau + GENERIC)]
        self.placed += new
        return new[-1][1]

    def backchannel(self, t, dur=CLIP):
        tau = round(t + self.delay, 2)
        self.clips.add(tau)
        self.placed.append((tau, tau + dur))
        return tau + dur


def run(call, sc, delay, take, bc, cool):
    """Closed loop. take/bc: what the policy would do at each frame from the human audio alone. Own state added
    here: nothing while the AI's sound is pending or playing; a take needs human speech since the AI's last reply
    ended; a backchannel needs cool s since the last one and 0.5 s of AI quiet (A10)."""
    sim, t, lv, n = Sim(call, sc, delay), call.t, call.last_voiced, len(call.t)
    ai_end = max([e for _, e in sc.opening], default=-1.0)
    last_bc, i = -np.inf, int(np.searchsorted(t, ai_end, side="right"))
    while i < n:
        ok = take[i:] & (lv[i:] > ai_end)
        tk = i + int(np.argmax(ok)) if ok.any() else n
        b0 = max(i, int(np.searchsorted(t, max(last_bc + cool, ai_end + 0.5))))
        bk = b0 + int(np.argmax(bc[b0:])) if b0 < n and bc[b0:].any() else n
        if tk == bk == n:
            break
        if tk <= bk:
            end = ai_end = sim.take(t[tk])
        else:
            end, last_bc = sim.backchannel(t[bk]), t[bk]
        i = int(np.searchsorted(t, end, side="right"))
    return merge(sim.placed), sim.clips


def score(call, spurts, clips=()):
    """dessa_score's per-call view, reduced to what pooling needs, plus which floor-takes were backchannel clips."""
    r = ds.per_call(call.name, spurts, call.human, call.dessa)
    return {"s": np.array([s for s, _, _ in r["takes"]]), "o": np.array([o for _, o, _ in r["takes"]]),
            "false": np.array([f for _, _, f in r["takes"]], bool), "bcs": np.array(r["bcs"]),
            "clip": np.array([round(s, 2) in clips for s, _, _ in r["takes"]], bool),
            "missed": r["missed"], "answered": r["dessa_answered"], "minutes": call.seconds / 60}


def score_d(med, over1, bcm, bc):
    """dessa_score's D; without its backchannel term when bc is False (take decisions alone: an AI reply the human
    talks straight over also counts as a 'backchannel' in dessa_timing, so that term would reward cut-ins)."""
    return ds.score_d(med, over1, bcm if bc else ds.TARGET["backchannels_per_min"])


def pooled(rows, bc=True):
    o = np.concatenate([r["o"] for r in rows])
    if not len(o):
        return None
    minutes = sum(r["minutes"] for r in rows)
    med, over1, bcm = float(np.median(o)), 100 * float(np.mean(o > 1)), sum(len(r["bcs"]) for r in rows) / minutes
    false = np.concatenate([r["false"] for r in rows])
    clip = np.concatenate([r["clip"] for r in rows])
    fci = 100 * float(np.mean(false))
    d = score_d(med, over1, bcm, bc)
    return {"floor_takes": int(len(o)), "median_s": med, "iqr_s": [float(np.percentile(o, 25)),
            float(np.percentile(o, 75))], "overlap_start_pct": 100 * float(np.mean(o < 0)), "over_1s_pct": over1,
            "backchannels_per_min": bcm, "D": d, "false_cut_in_pct": fci,
            "takes_that_are_clips_pct": 100 * float(np.mean(clip)),
            "false_cut_ins_from_clips_pct": 100 * float(np.sum(false & clip)) / max(1, int(np.sum(false))),
            "missed_take_pct": 100 * sum(r["missed"] for r in rows) / max(1, sum(r["answered"] for r in rows)),
            "J": d + fci / 100}


def blocks(rows):
    """10 s blocks of every call's timeline: (offsets, false flags, backchannels) per block."""
    out = []
    for r in rows:
        for k in range(int(np.ceil(r["minutes"] * 60 / BLOCK))):
            m = (r["s"] >= k * BLOCK) & (r["s"] < (k + 1) * BLOCK)
            out.append((r["o"][m], r["false"][m], int(np.sum((r["bcs"] >= k * BLOCK) & (r["bcs"] < (k + 1) * BLOCK)))))
    return out


def dj(bl, pick, bc):
    """(D, J, false cut-in fraction) of the picked blocks."""
    o = np.concatenate([bl[j][0] for j in pick])
    if not len(o):
        return np.nan, np.nan, np.nan
    bcm = sum(bl[j][2] for j in pick) / (len(pick) * BLOCK / 60)
    d = score_d(float(np.median(o)), 100 * float(np.mean(o > 1)), bcm, bc)
    f = float(np.mean(np.concatenate([bl[j][1] for j in pick])))
    return d, d + f, f


def bootstrap(per_policy, bases, bc, n_boot=2000):
    """95% CIs of D and J per policy, and of J and false cut-in % minus each base policy's, resampling the same
    10 s blocks for every policy (paired: all policies replay the same human timeline)."""
    bls = {k: blocks(v) for k, v in per_policy.items()}
    nb = len(next(iter(bls.values())))
    rng, acc = np.random.default_rng(0), {k: [] for k in bls}
    for _ in range(n_boot):
        pick = rng.integers(0, nb, nb)
        for k, bl in bls.items():
            acc[k].append(dj(bl, pick, bc))
    out = {}
    ci = lambda v, s=1: [round(s * float(np.nanpercentile(v, 2.5)), 3), round(s * float(np.nanpercentile(v, 97.5)), 3)]
    for k, a in acc.items():
        a = np.array(a)
        out[k] = {"D_ci95": ci(a[:, 0]), "J_ci95": ci(a[:, 1])}
        for base in bases:
            if k != base:
                b = np.array(acc[base])
                out[k]["dJ_vs_" + base] = ci(a[:, 1] - b[:, 1])
                out[k]["dFCI_pct_vs_" + base] = ci(a[:, 2] - b[:, 2], 100)
    return out


# ---------------------------------------------------------------- models

def sigmoid(z):
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


class Logistic:
    """L2 logistic regression (Newton steps) on the features plus hinge terms of the two quiet clocks."""

    def __init__(self, knots=(0.1, 0.2, 0.4, 0.8), drop=()):
        self.knots, self.drop = knots, drop

    def design(self, X):
        keep = [k for k in range(X.shape[1]) if k not in self.drop]
        h = [np.maximum(X[:, c:c + 1] - k, 0) for c in (QI, LQI) for k in self.knots]
        return np.hstack([X[:, keep]] + h)

    def fit(self, X, y, w, lam=1.0):
        Z = self.design(X)
        self.mu, self.sd = Z.mean(0), Z.std(0) + 1e-6
        Z = np.hstack([np.ones((len(Z), 1)), (Z - self.mu) / self.sd])
        self.beta = np.zeros(Z.shape[1])
        reg = lam * np.r_[0, np.ones(Z.shape[1] - 1)]
        for _ in range(30):
            p = sigmoid(Z @ self.beta)
            g = Z.T @ (w * (p - y)) + reg * self.beta
            H = (Z * (w * p * (1 - p))[:, None]).T @ Z + np.diag(reg + 1e-6)
            step = np.linalg.solve(H, g)
            self.beta -= step
            if np.abs(step).max() < 1e-6:
                break
        return self

    def predict(self, X):
        Z = (self.design(X) - self.mu) / self.sd
        return sigmoid(self.beta[0] + Z @ self.beta[1:])


class GBM:
    """Gradient-boosted depth-3 trees on 32-bin histograms, logistic loss (numpy only: no sklearn on the Studio)."""

    def __init__(self, rounds=200, depth=3, lr=0.05, bins=32, l2=1.0, min_h=2.0):
        self.rounds, self.depth, self.lr, self.bins, self.l2, self.min_h = rounds, depth, lr, bins, l2, min_h

    def binned(self, X):
        return np.stack([np.searchsorted(e, X[:, f], side="right") for f, e in enumerate(self.edges)], 1)

    def fit(self, X, y, w):
        q = np.linspace(0, 1, self.bins + 1)[1:-1]
        self.edges = [np.unique(np.quantile(X[:, f], q)) for f in range(X.shape[1])]
        B = self.binned(X)
        p0 = float(np.clip(np.sum(w * y) / np.sum(w), 1e-4, 1 - 1e-4))
        self.base, f, self.trees = np.log(p0 / (1 - p0)), None, []
        f = np.full(len(y), self.base)
        self.gain = np.zeros(X.shape[1])
        for _ in range(self.rounds):
            p = sigmoid(f)
            tree = self.tree(B, w * (p - y), w * p * (1 - p))
            self.trees.append(tree)
            f += self.lr * self.apply(B, tree)
        return self

    def tree(self, B, g, h):
        n, F = B.shape
        nb, node = self.bins, np.zeros(n, np.int64)
        feats, thrs = [], []
        for d in range(self.depth):
            m = 2 ** d
            key = ((node[:, None] * F + np.arange(F)) * nb + B).ravel()
            G = np.bincount(key, np.repeat(g, F), m * F * nb).reshape(m, F, nb)
            H = np.bincount(key, np.repeat(h, F), m * F * nb).reshape(m, F, nb)
            GL, HL = np.cumsum(G, 2)[:, :, :-1], np.cumsum(H, 2)[:, :, :-1]
            Gt, Ht = G.sum(2, keepdims=True), H.sum(2, keepdims=True)
            GR, HR = Gt - GL, Ht - HL
            gain = GL ** 2 / (HL + self.l2) + GR ** 2 / (HR + self.l2) - Gt ** 2 / (Ht + self.l2)
            gain[(HL < self.min_h) | (HR < self.min_h)] = -np.inf
            best = gain.reshape(m, -1).argmax(1)
            f_, t_ = best // (nb - 1), best % (nb - 1)
            ok = gain.reshape(m, -1)[np.arange(m), best] > 0
            t_ = np.where(ok, t_, nb)  # no useful split: everything goes left
            np.add.at(self.gain, f_[ok], gain.reshape(m, -1)[np.arange(m), best][ok])
            feats.append(f_)
            thrs.append(t_)
            node = 2 * node + (B[np.arange(n), f_[node]] > t_[node])
        leaves = 2 ** self.depth
        Gl, Hl = np.bincount(node, g, leaves), np.bincount(node, h, leaves)
        return feats, thrs, -Gl / (Hl + self.l2)

    def apply(self, B, tree):
        feats, thrs, val = tree
        node = np.zeros(len(B), np.int64)
        for f_, t_ in zip(feats, thrs):
            node = 2 * node + (B[np.arange(len(B)), f_[node]] > t_[node])
        return val[node]

    def predict(self, X):
        B = self.binned(X)
        return sigmoid(self.base + self.lr * sum(self.apply(B, t) for t in self.trees))


POS_W = 20.0  # positive-frame weight: thresholds are tuned afterwards, this only spreads the scores


def cols(c, text):
    return np.hstack([c.X, c.T]) if text else c.X


def oof(calls, scripts, delay, make, text):
    """Leave-one-call-out scores: p[name] = (take, backchannel) from models trained on the other calls."""
    lab = {c.name: fd.labels(c, scripts[c.name], delay) for c in calls}
    p, info = {}, []
    for c in calls:
        rest = [o for o in calls if o is not c]
        heads = []
        for head in (0, 2):  # (take, take_ok), (bc, bc_ok)
            X = np.concatenate([cols(o, text)[lab[o.name][head + 1]] for o in rest])
            y = np.concatenate([lab[o.name][head][lab[o.name][head + 1]] for o in rest]).astype(float)
            heads.append(make().fit(X, y, np.where(y > 0, POS_W, 1.0)))
        p[c.name] = tuple(m.predict(cols(c, text)) for m in heads)
        info.append(heads)
    return p, lab, info


def auc(y, s):
    """Mann-Whitney AUC with average ranks for ties."""
    y, s = np.asarray(y, bool), np.asarray(s, float)
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    rank = (np.cumsum(cnt) - (cnt - 1) / 2)[inv]
    npos = y.sum()
    return float((rank[y].sum() - npos * (npos + 1) / 2) / (npos * (len(y) - npos)))


PAUSE_Q = 0.2  # the pause decision point: 0.2 s of quiet + 0.25 s delay lands near the 0.43 s target gap


def pauses(c, sc, quiet=PAUSE_Q):
    """Pause decision points of one call: (frame index, Dessa took the floor) at `quiet` s into every human pause
    that lasts that long, unless the human talkspurt began while Dessa was talking (a backchannel or barge-in)."""
    answered, out = {r.answers for r in sc.replies}, []
    for j, (s, e) in enumerate(c.human):
        nxt = c.human[j + 1][0] if j + 1 < len(c.human) else np.inf
        i = int(round((e + quiet) / fd.HOP)) - 1
        if nxt - e > quiet and i < len(c.t) and not any(a <= s < b for a, b in c.dessa):
            out.append((i, j in answered))
    return out


def separability(calls, scripts, p, text):
    """At PAUSE_Q s into each pause, how well does a score rank Dessa's turn ends above the pauses where the human
    went on? AUC 0.5 = no signal."""
    y, model, feats = [], [], []
    for c in calls:
        for i, end in pauses(c, scripts[c.name]):
            y.append(end)
            model.append(p[c.name][0][i])
            feats.append(cols(c, text)[i])
    feats = np.array(feats)
    names = fd.FEATURES + (fd.TEXT_FEATURES if text else ())
    single = {n: round(auc(y, feats[:, k]), 3) for k, n in enumerate(names)}
    top = dict(sorted(single.items(), key=lambda kv: -abs(kv[1] - 0.5))[:5])
    return {"pauses": len(y), "turn_end_pct": round(100 * float(np.mean(y)), 1), "quiet_s": PAUSE_Q,
            "auc_model": round(auc(y, model), 3), "auc_top_features": top}


def gate_oof(calls, scripts, text):
    """A pause classifier (turn end vs hold at PAUSE_Q s of quiet), leave-one-call-out -> p[name] = (per-frame
    turn-end probability, zeros). The constant quiet clock is dropped; the Level clock still varies at that point."""
    rows = {c.name: pauses(c, scripts[c.name]) for c in calls}
    p = {}
    for c in calls:
        rest = [o for o in calls if o is not c]
        X = np.concatenate([cols(o, text)[[i for i, _ in rows[o.name]]] for o in rest])
        y = np.concatenate([[end for _, end in rows[o.name]] for o in rest]).astype(float)
        m = Logistic(knots=(), drop=(QI, fd.FEATURES.index("log_quiet"))).fit(X, y, np.ones(len(y)))
        p[c.name] = (m.predict(cols(c, text)), np.zeros(len(c.t)))
    return p


# ---------------------------------------------------------------- policies

def oracle_wishes(c, sc, delay):
    t, take, bc = c.t, np.zeros(len(c.t), bool), np.zeros(len(c.t), bool)
    starts = [s for s, _ in c.human] + [np.inf]
    for r in sc.replies:
        e = c.human[r.answers][1]
        take |= (t >= e) & (t < starts[r.answers + 1])
    for s, _ in sc.bcs:
        bc[min(len(t) - 1, max(0, int(round((s - delay) / fd.HOP)) - 1))] = True
    return take, bc


def a10(c, human, talk, quiet, cool):
    """A10's hand-tuned backchannel trigger (bc_sim.triggers) on the human track; AI gating happens in run()."""
    w = np.zeros(len(c.t), bool)
    ts = bc_sim.triggers(human, np.zeros_like(human), quiet, talk, cool)
    w[np.minimum((np.array(ts) / fd.HOP).astype(int), len(w) - 1)] = True
    return w


def grid_select(calls, sims, combos, bc):
    """Per held-out call: the combo with the lowest pooled J on the other calls. -> {name: combo index}."""
    pick = {}
    for c in calls:
        best, arg = np.inf, None
        for k in range(len(combos)):
            p = pooled([sims[k][o.name] for o in calls if o is not c], bc)
            if p and p["J"] < best:
                best, arg = p["J"], k
        pick[c.name] = arg
    return pick


def sweep(calls, scripts, delay, combos, wishes):
    """sims[k][name] = score of combo k on call name."""
    sims = []
    for combo in combos:
        row = {}
        for c in calls:
            take, bc, cool = wishes(c, combo)
            row[c.name] = score(c, *run(c, scripts[c.name], delay, take, bc, cool))
        sims.append(row)
    return sims


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.25,
                    help="decision -> sound at the ear: A5 uplink 0.12 s + A10 adapter->recorder 0.13 s (measured)")
    ap.add_argument("--policies", default="rules,rules*,gate,gate+text,logistic,gbm,logistic+text,gbm+text")
    ap.add_argument("--no-bc", action="store_true", help="no backchannels from any policy: compare take decisions alone")
    a = ap.parse_args()
    bc = not a.no_bc
    t0 = time.time()
    calls = [fd.load(n) for n in fd.names()]
    scripts = {c.name: fd.script(c.human, c.dessa) for c in calls}
    rows, chosen = {}, {}

    # identity: Dessa's own timing through the simulator must give back Dessa's track exactly
    rows["dessa"] = []
    for c in calls:
        sim = Sim(c, scripts[c.name], a.delay)
        for r in scripts[c.name].replies:
            sim.take(r.onset - a.delay)
        for s, e in scripts[c.name].bcs:
            sim.backchannel(s - a.delay, e - s)
        got = merge(sim.placed)
        assert got == [(round(s, 2), round(e, 2)) for s, e in c.dessa], c.name
        rows["dessa"].append(score(c, got))
    rows["oracle"] = []
    for c in calls:
        take, clip = oracle_wishes(c, scripts[c.name], a.delay)
        rows["oracle"].append(score(c, *run(c, scripts[c.name], a.delay, take, clip & bc, 0.0)))
    want = a.policies.split(",")

    if {"rules", "rules*", "gate", "gate+text"} & set(want):
        humans = {c.name: fd.tracks(c.name)[0] for c in calls}
        bcs = {} if a.no_bc else {
            (talk, quiet, cool): {c.name: a10(c, humans[c.name], talk, quiet, cool) for c in calls}
            for talk, quiet, cool in itertools.product((1.0, 2.0, 3.0), (0.02, 0.04, 0.1), (3.0, 6.0))}
        del humans
        off = {c.name: np.zeros(len(c.t), bool) for c in calls}

        def rule_wishes(c, combo):
            det, q, b = combo
            return c.X[:, LQI if det == "level" else QI] >= q, (bcs[b] if b else off)[c.name], 0.0

        if "rules" in want:
            deployed = ("level", 0.3, None if a.no_bc else (3.0, 0.04, 6.0))
            rows["rules"] = [sweep([c], scripts, a.delay, [deployed], rule_wishes)[0][c.name] for c in calls]
        if "rules*" in want:
            combos = list(itertools.product(("level", "voiced"), np.round(np.arange(0.1, 0.81, 0.05), 2),
                                            [None] + list(bcs)))
            sims = sweep(calls, scripts, a.delay, combos, rule_wishes)
            pick = grid_select(calls, sims, combos, bc)
            rows["rules*"] = [sims[pick[c.name]][c.name] for c in calls]
            chosen["rules*"] = {c.name: str(combos[pick[c.name]]) for c in calls}
        print(f"rules done {time.time() - t0:.0f}s", file=sys.stderr)

    # gate: the rules* family, plus an early take at q1 s of quiet when a pause classifier says turn end
    for name, text in (("gate", False), ("gate+text", True)):
        if name not in want or (text and any(c.T is None for c in calls)):
            continue
        p = gate_oof(calls, scripts, text)
        chosen[name + "_separability"] = separability(calls, scripts, p, text)
        combos = list(itertools.product((0.1, 0.15, 0.2, 0.25), (0.55, 0.65, 0.75, 0.85), (0.4, 0.6, 0.9, 1.3),
                                        [None] + list(bcs)))

        def gate_wishes(c, combo, p=p):
            q1, th, q2, b = combo
            q = c.X[:, QI]
            return ((q >= q1) & (p[c.name][0] >= th)) | (q >= q2), (bcs[b] if b else off)[c.name], 0.0

        sims = sweep(calls, scripts, a.delay, combos, gate_wishes)
        pick = grid_select(calls, sims, combos, bc)
        rows[name] = [sims[pick[c.name]][c.name] for c in calls]
        chosen[name] = {c.name: str(combos[pick[c.name]]) for c in calls}
        print(f"{name} done {time.time() - t0:.0f}s {chosen[name + '_separability']}", file=sys.stderr)

    for name, make, text in (("logistic", Logistic, False), ("gbm", GBM, False),
                             ("logistic+text", Logistic, True), ("gbm+text", GBM, True)):
        if name not in want:
            continue
        if text and any(c.T is None for c in calls):
            print(f"{name}: skipped, run bench/floor_asr.py first", file=sys.stderr)
            continue
        p, lab, info = oof(calls, scripts, a.delay, make, text)
        chosen[name + "_separability"] = separability(calls, scripts, p, text)
        # one threshold grid for all folds: quantiles of the out-of-fold scores on decision frames (no labels used)
        qs = 1 - np.geomspace(0.002, 0.15, 12)
        tg = np.quantile(np.concatenate([p[c.name][0][lab[c.name][1]] for c in calls]), qs)
        bg = np.quantile(np.concatenate([p[c.name][1][lab[c.name][3]] for c in calls]), qs[::2])
        combos = list(itertools.product(range(len(tg)), (0.6, 1.0, 2.0),
                                        [None] + ([] if a.no_bc else list(range(len(bg)))), (2.0, 5.0)))
        combos = [k for k in combos if k[2] is not None or k[3] == 2.0]

        def wishes(c, combo, p=p, tg=tg, bg=bg):
            ti, fb, bi, cool = combo
            q = c.X[:, QI]
            bc = (p[c.name][1] >= bg[bi]) & (q < 0.2) if bi is not None else np.zeros(len(q), bool)
            return (p[c.name][0] >= tg[ti]) | (q >= fb), bc, cool

        sims = sweep(calls, scripts, a.delay, combos, wishes)
        pick = grid_select(calls, sims, combos, bc)
        rows[name] = [sims[pick[c.name]][c.name] for c in calls]
        chosen[name] = {c.name: str(combos[pick[c.name]]) for c in calls}
        if make is GBM:
            g = np.mean([m.gain / m.gain.sum() for heads in info for m in heads[:1]], axis=0)
            names = fd.FEATURES + (fd.TEXT_FEATURES if text else ())
            chosen[name + "_take_gain"] = dict(sorted(zip(names, np.round(g, 3).tolist()), key=lambda x: -x[1]))
        print(f"{name} done {time.time() - t0:.0f}s {chosen[name + '_separability']}", file=sys.stderr)

    bases = [b for b in ("rules", "rules*") if b in rows]
    boot = bootstrap(rows, bases, bc)
    out = {"delay_s": a.delay, "calls": len(calls), "minutes": round(sum(c.seconds for c in calls) / 60, 1),
           "objective": "J = D + false_cut_in_pct / 100, leave-one-call-out" + ("" if bc else
                        "; no backchannels, D without its backchannel term"), "policies": {}, "chosen": chosen}
    print(f"delay {a.delay}s, {len(calls)} calls, {out['minutes']} min; {out['objective']}; "
          f"CIs: paired 10 s block bootstrap")
    print(f"{'policy':13s} {'takes':>5s} {'median':>6s} {'iqr':>13s} {'ovl%':>5s} {'>1s%':>5s} {'bc/m':>5s} "
          f"{'D':>5s} {'fci%':>5s} {'clip%':>5s} {'miss%':>5s} {'J':>5s} {'J ci95':>14s} "
          + " ".join(f"{'dJ vs ' + b:>18s} {'dfci% vs ' + b:>18s}" for b in bases))
    for k, r in rows.items():
        p = pooled(r, bc)
        out["policies"][k] = {**{x: (round(v, 3) if isinstance(v, float) else v) for x, v in p.items()}, **boot[k]}
        deltas = " ".join(f"{str(boot[k].get('dJ_vs_' + b, '')):>18s} {str(boot[k].get('dFCI_pct_vs_' + b, '')):>18s}"
                          for b in bases)
        print(f"{k:13s} {p['floor_takes']:5d} {p['median_s']:6.2f} {p['iqr_s'][0]:6.2f}..{p['iqr_s'][1]:5.2f} "
              f"{p['overlap_start_pct']:5.1f} {p['over_1s_pct']:5.1f} {p['backchannels_per_min']:5.2f} {p['D']:5.3f} "
              f"{p['false_cut_in_pct']:5.1f} {p['false_cut_ins_from_clips_pct']:5.1f} {p['missed_take_pct']:5.1f} "
              f"{p['J']:5.3f} {str(boot[k]['J_ci95']):>14s} {deltas}")
    print("clip% = share of false cut-ins that are backchannel clips landing after the human stopped")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"floor-eval-d{a.delay:.2f}{'-nobc' if a.no_bc else ''}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"{path} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()

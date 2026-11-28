"""C1-ctx-sem-r4: sem-r3 with ONE change, the key collision test (slot-class normalisation). Nothing in production
calls it. brief_compress_sem3.py (sem-r3), brief_compress_sem2.py (sem-r2) and brief_compress.py (r14) are untouched.

  sem-r3: two keys collide when their collision forms (sem-r2 `ckey`) are equal, or the shorter (>= 2 words) is a
          subset of the longer (sem-r2 `collides`).
  sem-r4: `collides4` = sem-r2's `collides` when it fires (same level); otherwise level 1 (subset level) when the
          final words of both collision forms map to the same canonical slot class (CLASS) AND the two keys share at
          least one non-class content word (not in CLASS, not in STOP). So "locker code" / "locker tag" collide (CODE,
          shared "locker"), "door code" / "access code" do not (no shared content word).

Keys, KEY_PROMPT, judge, JUDGE_PROMPT, the per-conversation cache, the partner rule (latest earlier colliding
candidate, an equal collision preferred over a subset one), the soft budget rule and the trim levels are sem-r2 /
sem-r3's, unchanged. `KeyCache4.arrive` is sem-r2's `KeyCache.arrive` with `partner4`. Pure stdlib.
"""
import time

import brief_compress as bc   # r14 module: only the parse helpers (_size)
import brief_compress_sem as sem1   # sem-r1: statement() (unchanged)
import brief_compress_sem2 as sem2   # sem-r2: KeyCache, ckey, collides, parse_brief, statements (unchanged)
import brief_compress_sem3 as sem3   # sem-r3: trim_exchange, _greedy, SLACK (unchanged)

SLACK = sem3.SLACK
statements = sem2.statements
CLASS = {}
for _cls, _words in (
        ("CODE", "code number num no id identifier pin tag reference ref password passcode ticket serial"),
        ("TIME", "time slot hour date day schedule"),
        ("PLACE", "address location place venue spot"),
        ("NAME", "name username handle")):
    for _w in _words.split():
        CLASS[_w] = _cls
STOP = frozenset("my the a an of for new current old updated now".split())


def slot_class(key):
    """Canonical class of the key's final word (collision form), or None."""
    c = sem2.ckey(key)
    return CLASS.get(c[-1]) if c else None


def content_words(key):
    return {w for w in sem2.ckey(key) if w not in CLASS and w not in STOP}


def collides4(k1, k2):
    """0 = no collision, 2 = equal collision forms, 1 = subset (sem-r2) or same slot class + shared content word."""
    c = sem2.collides(k1, k2)
    if c:
        return c
    a = slot_class(k1)
    if a is None or a != slot_class(k2):
        return 0
    return 1 if content_words(k1) & content_words(k2) else 0


def partner4(keys, j):
    """sem-r2's partner rule with collides4: latest earlier colliding entry, equal preferred over level 1."""
    best = {2: None, 1: None}
    for i in range(j):
        c = collides4(keys[i], keys[j])
        if c:
            best[c] = i
    return best[2] if best[2] is not None else best[1]


class KeyCache4(sem2.KeyCache):
    """sem-r2's cache; on arrival the partner is found with collides4."""

    def arrive(self, statement, earlier):
        k, ks = self.add(statement)
        rec = {"key_s": ks, "judge_s": None, "verdict": None}
        keys = [self.lookup(s)[1] for s in earlier] + [k]
        p = partner4(keys, len(keys) - 1)
        if p is not None:
            rec["verdict"], rec["judge_s"] = self.judge_pair(earlier[p], statement)
        if ks is not None or rec["judge_s"] is not None:
            self.arrivals.append(rec)
        return rec


KeyCache = KeyCache4


def compress_brief_sem4_info(text, budget_chars, cache=None, judge=None, slack=SLACK):
    """sem-r3's compress_brief_sem3_info with partner4 in place of sem-r2's partner (nothing else differs)."""
    info = {"candidates": 0, "key_misses": 0, "judge_misses": 0, "collisions": 0, "judges": [], "replaced": 0,
            "reserved": 0, "trimmed": [], "untrimmed": 0, "keys": [], "fallback": None, "wall_s": 0.0}
    if budget_chars <= 0 or not text:
        return "", info
    if len(text) <= budget_chars:
        return text, info
    p = sem2.parse_brief(text, budget_chars)
    if p is None:
        return text[-budget_chars:], info
    header, exchanges, trailer, fact = p
    n = len(exchanges)
    filler = [i for i in reversed(range(n - 1)) if not fact[i]]
    idx = [i for i in range(n) if fact[i]]
    tail = filler[0::2] + filler[1::2] + ([] if fact[n - 1] else [n - 1])
    size = {i: (bc._size([exchanges[i]]), -i) for i in idx}
    r4_order = sorted(idx, key=size.__getitem__) + tail
    drop, rep = set(), {}
    t0 = time.monotonic()
    if cache is None:
        info["fallback"] = "no cache"
    else:
        stmts = {i: sem1.statement(exchanges[i]) for i in idx}
        cand = [i for i in idx if stmts[i]]
        info["candidates"] = len(cand)
        if not cand or len(cand) > sem2.MAX_CANDIDATES:
            info["fallback"] = "no candidates" if not cand else "too many candidates"
        else:
            keys = []
            for i in cand:
                hit, k = cache.lookup(stmts[i])
                if not hit:
                    info["key_misses"] += 1
                    try:
                        k = cache.add(stmts[i])[0]
                    except LookupError:
                        k = None
                keys.append(k)
            info["keys"] = [[i, stmts[i], k] for i, k in zip(cand, keys)]
            for j, i in enumerate(cand):
                pj = partner4(keys, j)
                if pj is None:
                    continue
                info["collisions"] += 1
                m = cand[pj]
                hit, v = cache.verdict(stmts[m], stmts[i])
                if not hit:
                    info["judge_misses"] += 1
                    v = cache.judge_pair(stmts[m], stmts[i], judge)[0]
                info["judges"].append([m, i, v])
                if v == "replace":
                    drop.add(m)
                    rep.setdefault(i, []).append(m)
    info["wall_s"] = round(time.monotonic() - t0, 3)
    info["replaced"] = len(drop)
    if not drop:   # r4 byte for byte
        keep = sem3._greedy(header, exchanges, trailer, r4_order, budget_chars)
        chunks = [header] + [exchanges[j] for j in sorted(keep)] + [trailer]
        return "\n".join(ln for c in chunks for ln in c), info
    eff = {i: exchanges[i] for i in range(n)}
    live = [i for i in idx if i not in drop]
    sup = sorted(i for i in rep if i not in drop)
    for i in sup:
        seen, todo = set(), list(rep[i])
        while todo:
            m = todo.pop()
            if m not in seen:
                seen.add(m)
                todo += rep.get(m, [])
        slot = min(seen)
        level, ex = sem3.trim_exchange(exchanges[i], bc._size([exchanges[slot]]), slack)
        if ex is None or eff[slot] is not exchanges[slot]:
            info["untrimmed"] += 1
            continue
        eff[slot] = ex
        live = [j for j in live if j != i] + [slot]
        info["trimmed"].append([i, slot, level, bc._size([ex]), bc._size([exchanges[slot]])])
    seq = [eff[i] for i in range(n)]
    order = sorted(set(live), key=lambda i: (bc._size([seq[i]]), -i)) + tail
    keep = sem3._greedy(header, seq, trailer, order, budget_chars)
    chunks = [header] + [seq[j] for j in sorted(keep)] + [trailer]
    return "\n".join(ln for c in chunks for ln in c), info


def compress_brief_sem4(text, budget_chars, cache=None, judge=None):
    return compress_brief_sem4_info(text, budget_chars, cache, judge)[0]

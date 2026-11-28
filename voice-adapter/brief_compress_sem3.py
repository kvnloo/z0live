"""C1-ctx-sem-r3: sem-r2's cached keys + collision judge, WITHOUT the static reservation, with a SOFT budget rule.
Nothing in production calls it. brief_compress_sem2.py (sem-r2) and brief_compress.py (r14) are untouched.

compress_brief_sem3_info(text, budget_chars, cache=None, judge=None) differs from sem-r2 in ONE place, the selection
after the verdicts are known (keys, collisions, judge, cache and fallback are sem-r2's own functions, unchanged):

  sem-r2: superseding line kept at its own position (size = shortest of itself and what it replaced) and every static
          fact r4 keeps is RESERVED before the greedy fill (hard reservation).
  sem-r3: no reservation. A superseding line (a candidate whose REPLACE verdict dropped at least one older statement)
          is emitted TRIMMED to its value-bearing clause and placed in the position of the oldest statement it
          (transitively) replaced, never longer than that older exchange + SLACK chars (the SOFT budget rule). If no
          trim level fits, the line stays at its own position at its own size (no reservation, plain r4 order).
          Everything else is r4 order (fact exchanges shortest first, equal sizes newest first, filler tail).

Trim levels (deterministic, `trim_exchange`; the first level that fits the slot is used):
  1. the asker's value-bearing sentence (sem-r1 `statement`: first sentence holding a word with a digit, padding and
     any later sentences dropped) + the exchange's reply lines unchanged;
  2. that sentence alone (reply lines dropped);
  3. the value-bearing clause: the comma / semicolon / colon separated part of that sentence holding its last word
     with a digit (so "I switched to a family plan, my membership ID is now Alpha-108." -> "my membership ID is now
     Alpha-108.").
The asker label ("user: ") is kept. Fallback to r4 byte for byte is sem-r2's (no cache, no candidates, too many
candidates, no REPLACE verdict, brief within budget). Pure stdlib.
"""
import re
import time

import brief_compress as bc   # r14 module: only the parse helpers (_size, _label)
import brief_compress_sem as sem1   # sem-r1: statement(), HAS_VALUE (unchanged)
import brief_compress_sem2 as sem2   # sem-r2: KeyCache, partner, parse_brief, statements (unchanged)

SLACK = 8   # chars a trimmed superseding exchange may exceed the slot of the older exchange it replaces
CLAUSE = re.compile(r"\s*[,;:]\s+")
KeyCache = sem2.KeyCache
statements = sem2.statements


def value_clause(sentence):
    """The comma / semicolon / colon separated part of `sentence` holding its last word with a digit (or None)."""
    parts = [p for p in CLAUSE.split(sentence.strip()) if p]
    hit = [p for p in parts if sem1.HAS_VALUE.search(p)]
    return hit[-1].strip() if hit else None


def trim_levels(exchange):
    """Candidate trimmed forms of an exchange, shortest-information-loss first: [(level, exchange lines), ...]."""
    line = exchange[0]
    lab = bc._label(line) or bc._label(line, bc.SPEAKER_ANY_CASE)
    prefix = f"{lab}: " if lab else ""
    sent = sem1.statement(exchange)
    if not sent:
        return []
    out = [(1, [prefix + sent] + list(exchange[1:])), (2, [prefix + sent])]
    cl = value_clause(sent)
    if cl and cl != sent:
        out.append((3, [prefix + cl]))
    return out


def trim_exchange(exchange, slot_chars, slack=SLACK):
    """(level, trimmed exchange) of the first trim level whose size <= slot_chars + slack, else (None, None)."""
    for level, ex in trim_levels(exchange):
        if bc._size([ex]) <= slot_chars + slack:
            return level, ex
    return None, None


def _greedy(header, exchanges, trailer, order, budget_chars):
    keep = set()
    for i in order:
        trial = [header] + [exchanges[j] for j in sorted(keep | {i})] + [trailer]
        if bc._size([c for c in trial if c]) <= budget_chars:
            keep.add(i)
    return keep


def compress_brief_sem3_info(text, budget_chars, cache=None, judge=None, slack=SLACK):
    """(compressed text, info). info as sem-r2 plus: trimmed [[newer idx, slot idx, level, chars, slot chars]],
    untrimmed (superseding lines no trim level fitted), reserved = 0 always (no reservation)."""
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
                pj = sem2.partner(keys, j)
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
        keep = _greedy(header, exchanges, trailer, r4_order, budget_chars)
        chunks = [header] + [exchanges[j] for j in sorted(keep)] + [trailer]
        return "\n".join(ln for c in chunks for ln in c), info
    eff = {i: exchanges[i] for i in range(n)}   # position -> exchange lines (a slot may hold a trimmed newer line)
    live = [i for i in idx if i not in drop]
    sup = sorted(i for i in rep if i not in drop)
    for i in sup:   # chain a -> b -> c: c's slot is the oldest statement it (transitively) replaced
        seen, todo = set(), list(rep[i])
        while todo:
            m = todo.pop()
            if m not in seen:
                seen.add(m)
                todo += rep.get(m, [])
        slot = min(seen)
        level, ex = trim_exchange(exchanges[i], bc._size([exchanges[slot]]), slack)
        if ex is None or eff[slot] is not exchanges[slot]:   # no level fits, or the slot is already taken
            info["untrimmed"] += 1
            continue
        eff[slot] = ex
        live = [j for j in live if j != i] + [slot]
        info["trimmed"].append([i, slot, level, bc._size([ex]), bc._size([exchanges[slot]])])
    seq = [eff[i] for i in range(n)]
    order = sorted(set(live), key=lambda i: (bc._size([seq[i]]), -i)) + tail
    keep = _greedy(header, seq, trailer, order, budget_chars)
    chunks = [header] + [seq[j] for j in sorted(keep)] + [trailer]
    return "\n".join(ln for c in chunks for ln in c), info


def compress_brief_sem3(text, budget_chars, cache=None, judge=None):
    return compress_brief_sem3_info(text, budget_chars, cache, judge)[0]

"""C1-ctx-sem-r2: cached per-statement semantic keys + collision judge + static-displacement rule for compress_brief.
Nothing in production calls it.

compress_brief_sem2_info(text, budget_chars, cache=None, judge=None) is r4's compressor (bench/brief_compress_r4.py:
verbatim, in-order subset of lines, header and trailer always kept, fact exchanges shortest first, equal sizes newest
first, filler spread, filler newest exchange last) with a mechanism BUNDLE of three parts (disclosed in the PREREG):

  1. CACHED KEYS. Every fact exchange whose asker text states a value (a word with a digit) is a candidate; its
     statement is sem-r1's (first sentence holding such a word, at most 160 chars). Each statement is keyed ONCE, by
     its own model call, when it arrives (off the voice path); the key is cached under sha256 of the normalised
     statement (KeyCache.add). At brief time keys are cache lookups; a miss is keyed then and counted.
  2. COLLISION JUDGE. Two keys collide when their collision forms (ckey: digit words dropped, a plural "s" dropped)
     are equal, or the shorter one has at least 2 words and all of them are in the longer one. Each candidate's
     PARTNER is the latest earlier candidate whose key collides with its own (an equal key preferred over a subset).
     Only on a collision, one short model call judges (partner, candidate) -> "replace" / "also"; the verdict is cached
     under sha256 of the pair. The harness judges on arrival of the newer statement (KeyCache.arrive, off the voice
     path); at brief time a verdict is a lookup, a miss is judged then and counted. REPLACE drops the partner; ALSO
     (or an unparsable reply, or a failed call) keeps both. A candidate that replaced at least one statement is a
     superseding line, tried at the shortest size of itself and the statements it replaced (as r7 / sem-r1).
  3. DISPLACEMENT RULE. A superseding line may not displace a static fact: every fact exchange that r4 would keep and
     that is neither dropped by a REPLACE nor superseding is reserved first; the greedy fill then runs in the sem order
     over the rest.

Fallback (byte-identical to r4): no cache, no candidates, more than MAX_CANDIDATES, no REPLACE verdict, or a brief
within budget. A keyer or judge exception is local (key None / "also") and is reported. Deterministic when the model
calls are (temperature 0, seed 42, no prompt cache). Pure stdlib.
"""
import hashlib
import json
import re
import time
import unicodedata
import urllib.request

import brief_compress as bc   # r14 module: only the parse helpers (_split, _size, _label)
import brief_compress_sem as sem1   # sem-r1: statement(), _norm() (re-used unchanged)

MAX_CANDIDATES = 24
KEY_PROMPT = (
    "Below is one thing a person said about themselves. Write a short key of 1 to 3 lowercase words naming the code "
    "or number that it gives, saying what that code or number is for, such as \"shed padlock code\", \"bus pass "
    "number\" or \"warranty ticket\". Use the plainest common words for the thing, so that a later statement giving a "
    "replacement value for the same thing would get the same key. Answer with the key only.\n\nStatement: ")
JUDGE_PROMPT = (
    "A person said two things, the older one first.\nOLDER: {old}\nNEWER: {new}\n"
    "Does the NEWER statement give a replacement for the code or number in the OLDER one, for the very same purpose, so "
    "the older value is no longer true? Then answer REPLACE. If both can still be true at once (two different things, "
    "even if similar), answer ALSO. Answer with one word.")


def norm_statement(s):
    """NFC, whitespace collapsed, stripped: the text whose sha256 is the cache key."""
    return " ".join(unicodedata.normalize("NFC", str(s)).split())


def stmt_hash(s):
    return hashlib.sha256(norm_statement(s).encode("utf-8")).hexdigest()


def pair_hash(older, newer):
    return hashlib.sha256((norm_statement(older) + "\n\x00\n" + norm_statement(newer)).encode("utf-8")).hexdigest()


def parse_key(reply):
    """First non-empty line of the reply, without a leading 'key:' label, normalised as sem-r1 (_norm); or None."""
    for ln in str(reply).splitlines():
        ln = re.sub(r"^\s*(key\s*[:\-]\s*)", "", ln.strip(), flags=re.I).strip(" \"'`*.")
        if ln:
            return sem1._norm(ln)
    return None


def parse_judge(reply):
    t = str(reply).upper()
    r, a = t.find("REPLACE"), t.find("ALSO")
    if r < 0 and a < 0:
        return None
    if a < 0 or (0 <= r < a):
        return "replace"
    return "also"


def ckey(key):
    """Collision form of a key: words without digits, a trailing plural 's' dropped from words over 3 letters."""
    if key is None:
        return ()
    ws = [w for w in str(key).split() if not any(c.isdigit() for c in w)]
    return tuple(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in ws)


def collides(k1, k2):
    """0 = no collision, 2 = equal collision forms, 1 = the shorter (>= 2 words) is a subset of the longer."""
    a, b = ckey(k1), ckey(k2)
    if not a or not b:
        return 0
    if a == b:
        return 2
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return 1 if len(small) >= 2 and set(small) <= set(big) else 0


def partner(keys, j):
    """Index (into keys) of the latest earlier entry colliding with keys[j] (equal preferred over subset), or None."""
    best = {2: None, 1: None}
    for i in range(j):
        c = collides(keys[i], keys[j])
        if c:
            best[c] = i
    return best[2] if best[2] is not None else best[1]


def _post(url, content, max_tokens, timeout, log):
    body = {"messages": [{"role": "user", "content": content}], "stream": False, "temperature": 0, "seed": 42,
            "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": False}
    req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    reply = data["choices"][0]["message"]["content"]
    if log is not None:
        log.append({"timings": {k: (data.get("timings") or {}).get(k)
                                for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")}, "reply": reply})
    return reply


def llm_key1(url, timeout=120.0, log=None):
    """key1(statement) -> normalised key or None: one greedy request for ONE statement."""
    def key1(statement):
        return parse_key(_post(url, KEY_PROMPT + statement, 16, timeout, log))
    return key1


def llm_judge(url, timeout=120.0, log=None):
    """judge(older, newer) -> "replace" / "also" / None: one greedy request per colliding pair."""
    def judge(older, newer):
        return parse_judge(_post(url, JUDGE_PROMPT.format(old=older, new=newer), 4, timeout, log))
    return judge


class KeyCache:
    """Per-conversation cache: sha256(normalised statement) -> key, sha256(pair) -> judge verdict.
    `key1` / `judge` are the model calls used on arrival (or on a miss)."""

    def __init__(self, key1=None, judge=None):
        self.key1, self.judge = key1, judge
        self.keys, self.verdicts = {}, {}
        self.arrivals = []   # per NEW statement: {"key_s", "judge_s" or None, "verdict"}
        self.errors = []

    def lookup(self, statement):
        h = stmt_hash(statement)
        return (True, self.keys[h]) if h in self.keys else (False, None)

    def add(self, statement):
        """Key a statement (no-op when cached); returns (key, wall seconds or None when cached)."""
        h = stmt_hash(statement)
        if h in self.keys:
            return self.keys[h], None
        if self.key1 is None:
            raise LookupError("no keyer")
        t0 = time.monotonic()
        try:
            k = self.key1(norm_statement(statement))
            k = None if k is None else sem1._norm(k)
        except Exception as exc:  # noqa: BLE001 - a failed key is a unique key, reported
            k = None
            self.errors.append(f"key {type(exc).__name__}: {str(exc)[:80]}")
        self.keys[h] = k
        return k, round(time.monotonic() - t0, 3)

    def verdict(self, older, newer):
        h = pair_hash(older, newer)
        return (True, self.verdicts[h]) if h in self.verdicts else (False, None)

    def judge_pair(self, older, newer, judge=None):
        """Judge (no-op when cached); returns (verdict, wall seconds or None when cached)."""
        h = pair_hash(older, newer)
        if h in self.verdicts:
            return self.verdicts[h], None
        fn = judge or self.judge
        t0 = time.monotonic()
        try:
            v = fn(norm_statement(older), norm_statement(newer)) if fn is not None else None
        except Exception as exc:  # noqa: BLE001 - a failed judge keeps both, reported
            v = None
            self.errors.append(f"judge {type(exc).__name__}: {str(exc)[:80]}")
        if fn is not None:
            self.verdicts[h] = v
        return v, round(time.monotonic() - t0, 3)

    def arrive(self, statement, earlier):
        """A new user statement arrives after `earlier` (statements in conversation order): key it, and if its key
        collides with an earlier one, judge it against its partner. Off the voice path. Returns the arrival record."""
        k, ks = self.add(statement)
        rec = {"key_s": ks, "judge_s": None, "verdict": None}
        keys = [self.lookup(s)[1] for s in earlier] + [k]
        p = partner(keys, len(keys) - 1)
        if p is not None:
            rec["verdict"], rec["judge_s"] = self.judge_pair(earlier[p], statement)
        if ks is not None or rec["judge_s"] is not None:
            self.arrivals.append(rec)
        return rec


def parse_brief(text, budget_chars):
    """(header, exchanges, trailer, fact flags) or None when r4 would not split the brief."""
    parts = bc._split(text.split("\n"))
    if parts is None:
        return None
    header, exchanges, trailer = parts
    if bc._size([header + trailer]) > budget_chars:
        return None
    fact = [len(ex) == 1 or len(" ".join(ex[1:])) <= bc.FACT_REPLY_CHARS for ex in exchanges]
    return header, exchanges, trailer, fact


def statements(text, budget_chars):
    """Candidate statements of a brief in exchange order (what arrives, one per user turn); [] if none."""
    p = parse_brief(text, budget_chars) if text and len(text) > budget_chars else None
    if p is None:
        return []
    _, exchanges, _, fact = p
    return [s for i, ex in enumerate(exchanges) if fact[i] for s in [sem1.statement(ex)] if s]


def _greedy(header, exchanges, trailer, order, budget_chars, reserved=()):
    keep = set(reserved)
    for i in order:
        if i in keep:
            continue
        trial = [header] + [exchanges[j] for j in sorted(keep | {i})] + [trailer]
        if bc._size([c for c in trial if c]) <= budget_chars:
            keep.add(i)
    return keep


def compress_brief_sem2_info(text, budget_chars, cache=None, judge=None, displacement_rule=True):
    """(compressed text, info). info: candidates, key_misses, judge_misses, collisions, judges [older idx, newer idx,
    verdict], replaced, reserved, fallback (None or reason), wall_s (the brief-time added wall: lookups + misses)."""
    info = {"candidates": 0, "key_misses": 0, "judge_misses": 0, "collisions": 0, "judges": [], "replaced": 0,
            "reserved": 0, "keys": [], "fallback": None, "wall_s": 0.0}
    if budget_chars <= 0 or not text:
        return "", info
    if len(text) <= budget_chars:
        return text, info
    p = parse_brief(text, budget_chars)
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
        if not cand or len(cand) > MAX_CANDIDATES:
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
                pj = partner(keys, j)
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
    sup = {i for i in rep if i not in drop}
    for i in rep:   # a chain a -> b -> c: c takes the shortest size of everything it (transitively) replaced
        if i in sup:
            seen, todo = set(), list(rep[i])
            while todo:
                m = todo.pop()
                if m not in seen:
                    seen.add(m)
                    todo += rep.get(m, [])
            size[i] = (min(bc._size([exchanges[m]]) for m in seen | {i}), -i)
    info["replaced"] = len(drop)
    if not drop:   # r4 byte for byte
        keep = _greedy(header, exchanges, trailer, r4_order, budget_chars)
    else:
        facts = sorted((i for i in idx if i not in drop), key=size.__getitem__)
        reserved = ()
        if displacement_rule:
            r4_keep = _greedy(header, exchanges, trailer, r4_order, budget_chars)
            reserved = tuple(i for i in sorted(r4_keep) if fact[i] and i not in drop and i not in sup)
            info["reserved"] = len(reserved)
        keep = _greedy(header, exchanges, trailer, facts + tail, budget_chars, reserved)
    chunks = [header] + [exchanges[j] for j in sorted(keep)] + [trailer]
    return "\n".join(ln for c in chunks for ln in c), info


def compress_brief_sem2(text, budget_chars, cache=None, judge=None):
    return compress_brief_sem2_info(text, budget_chars, cache, judge)[0]

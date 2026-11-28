"""C1-ctx-sem-r1: semantic supersession pass for compress_brief (model-keyed). Nothing in production calls it.

compress_brief_sem(text, budget_chars, keyer=None) is r4's compressor (bench/brief_compress_r4.py: verbatim, in-order
subset of lines, header and trailer always kept, fact exchanges shortest first, equal sizes newest first, filler
spread, filler newest exchange last) with ONE addition in the fact order:

  1. every fact exchange whose asker text states a value (a word with a digit) is a candidate; its statement is the
     first sentence of the asker text that holds such a word (at most STMT_CHARS chars);
  2. `keyer(statements)` (one model pass over ALL candidate statements of the brief) returns one short entity key per
     statement ("card number", "booking reference") or None; keys are normalised (_norm);
  3. candidates that share a key are one group: only its NEWEST member is kept, the older members are dropped, and the
     newest is tried at the shortest size among the group (as r7: so the stale member's size cannot take the budget
     and the newest is not crowded out by its own length). Candidates with a unique or missing key keep r4's key.

No lexical slot, cue word or attribute rule is used: r7-r14 read "my <subject> is" and update cue words, which covered
0 of 9 naturally phrased updates on the blind r15 set. Whether two statements are about the same thing is the model's
call.

Fallback (byte-identical to r4): keyer is None, keyer raises (HTTP error, timeout, no server), the reply holds no
parsable key, or the brief is already within budget. The pass is deterministic when the keyer is (llm_keyer: temperature
0, seed 42, no prompt cache, one request per brief). Pure stdlib; llm_keyer talks to a llama.cpp server's
/v1/chat/completions.
"""
import json
import re
import time
import urllib.request

import brief_compress as bc  # r14 module: only the parse helpers (_split, _size, _label); its supersession is not used

STMT_CHARS = 160
MAX_CANDIDATES = 24
HAS_VALUE = re.compile(r"\S*\d\S*")
SENTENCE = re.compile(r"(?<=[.!?])\s+")
STOP = frozenset(("my", "the", "a", "an", "your", "his", "her", "our", "their", "new", "old", "current", "updated"))
PROMPT = (
    "Each numbered line below is something a person said about themselves. For every line, write a short key of 1 to "
    "3 lowercase words naming the code or number that the line gives, saying what that code or number is for, such as "
    "\"shed padlock code\", \"bus pass number\" or \"warranty ticket\". Lines that give a replacement value for the "
    "same code or number must get exactly the same key; lines about different codes or numbers must get different "
    "keys, even when they are about the same object. Answer with one line per input line, formatted "
    "\"<number>: <key>\", and nothing else.\n\n")
LINE = re.compile(r"^\s*(\d+)\s*[:.)\-]\s*(.+?)\s*$")


def _norm(key):
    """Lowercase alphanumeric words without STOP words, joined by one space; None when nothing is left."""
    words = [w for w in re.sub(r"[^a-z0-9' ]+", " ", str(key).lower()).split() if w not in STOP]
    return " ".join(words) or None


def statement(exchange):
    """The candidate statement of an exchange, or None: the asker's first sentence that holds a value word."""
    line = exchange[0]
    lab = bc._label(line) or bc._label(line, bc.SPEAKER_ANY_CASE)
    text = line[len(lab) + 2:] if lab else line
    for sent in SENTENCE.split(text):
        if HAS_VALUE.search(sent):
            return sent.strip()[:STMT_CHARS]
    return None


def build_prompt(statements):
    return PROMPT + "\n".join(f"{i}. {s}" for i, s in enumerate(statements, 1))


def parse_keys(reply, n):
    """[key or None] * n from '<number>: <key>' lines; numbers outside 1..n and repeats are ignored."""
    out = [None] * n
    for ln in str(reply).splitlines():
        m = LINE.match(ln)
        if m and 1 <= int(m.group(1)) <= n and out[int(m.group(1)) - 1] is None:
            out[int(m.group(1)) - 1] = _norm(m.group(2))
    return out


def llm_keyer(url, timeout=180.0, log=None):
    """keyer(statements) -> keys, one greedy request (temperature 0, seed 42, no prompt cache) to a llama.cpp
    server at `url` (e.g. http://127.0.0.1:18091). `log` (a dict) receives the server timings."""
    def keyer(statements):
        body = {"messages": [{"role": "user", "content": build_prompt(statements)}], "stream": False,
                "temperature": 0, "seed": 42, "max_tokens": min(300, 16 * len(statements) + 16),
                "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": False}
        req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        if log is not None:
            log["timings"] = {k: (data.get("timings") or {}).get(k)
                              for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")}
            log["reply"] = data["choices"][0]["message"]["content"]
        return parse_keys(data["choices"][0]["message"]["content"], len(statements))
    return keyer


def _fact_order_sem(exchanges, idx, keys):
    """r4 order (size ascending, newest first on ties) with each key group collapsed to its newest member, tried at
    the shortest size of the group. keys: {exchange index: normalised key or None}."""
    size = {i: (bc._size([exchanges[i]]), -i) for i in idx}
    groups = {}
    for i in sorted(k for k in keys if keys[k] is not None):
        groups.setdefault(keys[i], []).append(i)
    drop = set()
    for ms in groups.values():
        if len(ms) > 1:
            size[ms[-1]] = (min(bc._size([exchanges[m]]) for m in ms), -ms[-1])
            drop.update(ms[:-1])
    return sorted((i for i in idx if i not in drop), key=size.__getitem__)


def compress_brief_sem_info(text, budget_chars, keyer=None):
    """(compressed text, info). info: candidates, grouped (dropped older members), keys, fallback (None or reason),
    wall_s (the keyer call). With keyer=None, or any keyer failure, the output is r4's byte for byte."""
    info = {"candidates": 0, "grouped": 0, "keys": [], "fallback": None, "wall_s": 0.0}
    if budget_chars <= 0 or not text:
        return "", info
    if len(text) <= budget_chars:
        return text, info
    parts = bc._split(text.split("\n"))
    if parts is None:
        return text[-budget_chars:], info
    header, exchanges, trailer = parts
    if bc._size([header + trailer]) > budget_chars:
        return text[-budget_chars:], info
    n = len(exchanges)
    fact = [len(ex) == 1 or len(" ".join(ex[1:])) <= bc.FACT_REPLY_CHARS for ex in exchanges]
    filler = [i for i in reversed(range(n - 1)) if not fact[i]]
    idx = [i for i in range(n) if fact[i]]
    keys = {}
    if keyer is None:
        info["fallback"] = "no keyer"
    else:
        stmts = {i: statement(exchanges[i]) for i in idx}
        cand = [i for i in idx if stmts[i]]
        info["candidates"] = len(cand)
        if not cand or len(cand) > MAX_CANDIDATES:
            info["fallback"] = "no candidates" if not cand else "too many candidates"
        else:
            t0 = time.monotonic()
            try:
                got = keyer([stmts[i] for i in cand])
                keys = {i: (None if k is None else _norm(k)) for i, k in zip(cand, got)}
                if all(k is None for k in keys.values()):
                    keys, info["fallback"] = {}, "no parsable key"
            except Exception as exc:  # noqa: BLE001 - any model-pass failure falls back to r4
                keys, info["fallback"] = {}, f"{type(exc).__name__}: {str(exc)[:80]}"
            info["wall_s"] = round(time.monotonic() - t0, 3)
            info["keys"] = [[i, stmts[i], keys.get(i)] for i in cand]
    facts = _fact_order_sem(exchanges, idx, keys)
    info["grouped"] = len(idx) - len(facts)
    order = facts + filler[0::2] + filler[1::2] + ([] if fact[n - 1] else [n - 1])
    keep = set()
    for i in order:
        trial = [header] + [exchanges[j] for j in sorted(keep | {i})] + [trailer]
        if bc._size([c for c in trial if c]) <= budget_chars:
            keep.add(i)
    chunks = [header] + [exchanges[j] for j in sorted(keep)] + [trailer]
    return "\n".join(ln for c in chunks for ln in c), info


def compress_brief_sem(text, budget_chars, keyer=None):
    return compress_brief_sem_info(text, budget_chars, keyer)[0]

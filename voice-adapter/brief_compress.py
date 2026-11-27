"""Deterministic lane-brief compressor (C56-live-compress). No model call, no fixture markers.

compress_brief(text, budget_chars) returns a verbatim, in-order subset of the brief's lines, at most budget_chars long:

  1. a brief already within budget is returned unchanged;
  2. the brief is split into header lines (before the first "Speaker: " line), exchanges (one asker line plus every
     line up to the next asker line; the asker is the first speaker seen) and trailer lines (after the last
     exchange, e.g. a Status line);
  3. header and trailer are always kept, then exchanges are added greedily in this priority order, each only if it
     still fits: "fact" exchanges shortest first (C1-ctx-compress-r4; equal sizes newest first; was: newest
     first), except that of two or more facts filling the same slot (the asker states the same attribute of their
     own, "my <subject> is ...", same normalised subject) only the newest is tried, at the best (shortest) priority
     of the slot's members, and the older ones are dropped (C1-ctx-compress-r7; was: every member tried by its own
     size, so a slightly shorter stale value could take the budget), then filler spread evenly over the chat (every other filler
     exchange counted back from the newest older filler, newest first; then the skipped fillers, newest first, only
     if budget is left; C56-live-compress-r3, was: all filler newest first), then a filler newest exchange last
     (C56-live-compress-r4, was: the newest exchange always kept first). An exchange is
     a fact when its reply is short (<= FACT_REPLY_CHARS) or it has no reply (C1-ctx-compress-r3): answers about the user's own plans, settings, numbers and
     permissions are one line, general-knowledge explanations are paragraphs;
  4. with no speaker structure, or when header + trailer alone exceed the budget, the newest budget_chars
     characters are kept (what tiers.snapshot already does at a larger cap).

It reproduces the idea of the hand-built GH11/C56 "e5" brief (all fact pairs, header, Status line, some filler)
from any live brief. Nothing in production calls it yet; tiers.snapshot and its 6000-char cap are unchanged.
"""
import re

SPEAKER = re.compile(r"^([A-Z][\w .'-]{0,30}): ")
# C1-ctx-compress-r2: lowercase labels ("user:"/"assistant:", as context_eval and raw transcripts write them) are
# speakers too. Used only when the capitalised parse finds no speaker pair, so capitalised briefs are byte-identical.
SPEAKER_ANY_CASE = re.compile(r"^([A-Z][\w .'-]{0,30}): ", re.IGNORECASE)
FACT_REPLY_CHARS = 200
# C1-ctx-compress-r7: a fact's slot is the subject of the asker's first own-attribute statement, "my <1-5 words>
# is/are/was/were" (case-insensitive, whitespace collapsed). Two facts with the same slot: the newer supersedes.
SLOT = re.compile(r"\bmy\s+((?:[a-z][\w'-]*\s+){0,4}?[a-z][\w'-]*)\s+(?:is|are|was|were)\b", re.IGNORECASE)


def _label(line, pattern=SPEAKER):
    m = pattern.match(line)
    return m.group(1) if m else None


def _split(lines):
    return _split_with(lines, SPEAKER) or _split_with(lines, SPEAKER_ANY_CASE)


def _split_with(lines, pattern):
    # the chat's two speakers are the first two distinct labels; other "Label: " lines (Status:, Note:) are not turns
    speakers = []
    for ln in lines:
        lab = _label(ln, pattern)
        if lab and lab not in speakers:
            speakers.append(lab)
        if len(speakers) == 2:
            break
    if len(speakers) < 2:
        return None
    idx = [i for i, ln in enumerate(lines) if _label(ln, pattern) in speakers]
    asker = speakers[0]
    starts = [i for i in idx if _label(lines[i], pattern) == asker]
    last = idx[-1] + 1
    exchanges = [lines[s:e] for s, e in zip(starts, starts[1:] + [last])]
    return lines[:starts[0]], exchanges, lines[last:]


def _size(chunks):
    n = sum(len(ln) for c in chunks for ln in c) + sum(len(c) for c in chunks)
    return n - 1 if n else 0  # newline joins


def _slot(exchange):
    """C1-ctx-compress-r7: normalised subject of the asker line's first "my <subject> is/are/was/were", else None."""
    line = exchange[0]
    lab = _label(line) or _label(line, SPEAKER_ANY_CASE)
    m = SLOT.search(line[len(lab) + 2:] if lab else line)
    return " ".join(m.group(1).lower().split()) if m else None


def _fact_order(exchanges, idx):
    """C1-ctx-compress-r7: supersession-aware fact order. Facts sharing a slot (_slot) keep only the newest member,
    tried at the shortest size among the slot's members; older members are dropped. Every other fact keeps the r4
    key: size ascending, newest first on equal sizes. With no shared slot the order is exactly r4's."""
    newest, best = {}, {}
    for i in idx:
        k = _slot(exchanges[i])
        if k is not None:
            newest[k] = max(newest.get(k, i), i)
            best[k] = min(best.get(k, _size([exchanges[i]])), _size([exchanges[i]]))
    keyed = []
    for i in idx:
        k = _slot(exchanges[i])
        if k is None:
            keyed.append(((_size([exchanges[i]]), -i), i))
        elif newest[k] == i:
            keyed.append(((best[k], -i), i))
    return [i for _, i in sorted(keyed)]


def compress_brief(text: str, budget_chars: int) -> str:
    if budget_chars <= 0 or not text:
        return ""
    if len(text) <= budget_chars:
        return text
    parts = _split(text.split("\n"))
    if parts is None:
        return text[-budget_chars:]
    header, exchanges, trailer = parts
    if _size([header + trailer]) > budget_chars:
        return text[-budget_chars:]
    n = len(exchanges)
    # C1-ctx-compress-r3: an asker line with no reply (a user statement nobody answered, e.g. "My vendor code is
    # Mint-57.") is a fact, not filler (was: len(ex) > 1 required, so reply-less statements were dropped first).
    fact = [len(ex) == 1 or len(" ".join(ex[1:])) <= FACT_REPLY_CHARS for ex in exchanges]
    filler = [i for i in reversed(range(n - 1)) if not fact[i]]  # newest first
    # C56-live-compress-r3: filler spread evenly over the chat (e5 style): every other filler exchange counted back
    # from the newest filler, then the skipped ones (newest first) only if budget is still left.
    # C56-live-compress-r4: the newest exchange is no longer always kept first. A fact newest exchange is kept with
    # the facts; a filler newest exchange is tried last, only if budget is still left (was: order started with n - 1).
    # C1-ctx-compress-r4: facts are tried by density, shortest exchange first (stable sort, so equal sizes stay newest
    # first), so a one-line user statement is not crowded out by newer facts with long explanatory replies
    # (was: all facts newest first).
    facts = _fact_order(exchanges, [i for i in range(n) if fact[i]])
    order = facts + filler[0::2] + filler[1::2] + ([] if fact[n - 1] else [n - 1])
    keep = set()
    for i in order:
        trial = [header] + [exchanges[j] for j in sorted(keep | {i})] + [trailer]
        if _size([c for c in trial if c]) <= budget_chars:
            keep.add(i)
    chunks = [header] + [exchanges[j] for j in sorted(keep)] + [trailer]
    return "\n".join(ln for c in chunks for ln in c)

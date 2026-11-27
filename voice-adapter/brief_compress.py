"""Deterministic lane-brief compressor (C56-live-compress). No model call, no fixture markers.

compress_brief(text, budget_chars) returns a verbatim, in-order subset of the brief's lines, at most budget_chars long:

  1. a brief already within budget is returned unchanged;
  2. the brief is split into header lines (before the first "Speaker: " line), exchanges (one asker line plus every
     line up to the next asker line; the asker is the first speaker seen) and trailer lines (after the last
     exchange, e.g. a Status line);
  3. header and trailer are always kept, then exchanges are added greedily in this priority order, each only if it
     still fits: "fact" exchanges shortest first (C1-ctx-compress-r4; was: newest first) by size band
     (C1-ctx-compress-r6: facts within 10% of the band's shortest are tried newest first; was: equal sizes only),
     then filler spread evenly over the chat (every other filler
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
FACT_BAND = 0.10  # C1-ctx-compress-r6: facts within 10% of a band's shortest fact are tried newest first


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


def _fact_order(exchanges, idx):
    """C1-ctx-compress-r6: tiered fact key. Facts are cut into size bands, shortest band first; a band starts at the
    shortest fact not yet placed and holds every fact up to FACT_BAND (10%) longer than that one. Inside a band the
    facts are tried newest first, so of two near-equal facts the newer (which may supersede the older) wins.
    Was (r4): strict size ascending, newest first only on exactly equal sizes."""
    left = sorted(idx, key=lambda i: (_size([exchanges[i]]), -i))
    order = []
    while left:
        top = _size([exchanges[left[0]]]) * (1 + FACT_BAND)
        band = [i for i in left if _size([exchanges[i]]) <= top]
        order += sorted(band, reverse=True)
        left = [i for i in left if _size([exchanges[i]]) > top]
    return order


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

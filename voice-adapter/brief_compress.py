"""Deterministic lane-brief compressor (C56-live-compress). No model call, no fixture markers.

compress_brief(text, budget_chars) returns a verbatim, in-order subset of the brief's lines, at most budget_chars long:

  1. a brief already within budget is returned unchanged;
  2. the brief is split into header lines (before the first "Speaker: " line), exchanges (one asker line plus every
     line up to the next asker line; the asker is the first speaker seen) and trailer lines (after the last
     exchange, e.g. a Status line);
  3. header and trailer are always kept, then exchanges are added greedily in this priority order, each only if it
     still fits: the newest exchange, then "fact" exchanges newest first, then the rest newest first. An exchange is
     a fact when its reply is short (<= FACT_REPLY_CHARS): answers about the user's own plans, settings, numbers and
     permissions are one line, general-knowledge explanations are paragraphs;
  4. with no speaker structure, or when header + trailer alone exceed the budget, the newest budget_chars
     characters are kept (what tiers.snapshot already does at a larger cap).

It reproduces the idea of the hand-built GH11/C56 "e5" brief (all fact pairs, header, Status line, some filler)
from any live brief. Nothing in production calls it yet; tiers.snapshot and its 6000-char cap are unchanged.
"""
import re

SPEAKER = re.compile(r"^([A-Z][\w .'-]{0,30}): ")
FACT_REPLY_CHARS = 200


def _label(line):
    m = SPEAKER.match(line)
    return m.group(1) if m else None


def _split(lines):
    # the chat's two speakers are the first two distinct labels; other "Label: " lines (Status:, Note:) are not turns
    speakers = []
    for ln in lines:
        lab = _label(ln)
        if lab and lab not in speakers:
            speakers.append(lab)
        if len(speakers) == 2:
            break
    if len(speakers) < 2:
        return None
    idx = [i for i, ln in enumerate(lines) if _label(ln) in speakers]
    asker = speakers[0]
    starts = [i for i in idx if _label(lines[i]) == asker]
    last = idx[-1] + 1
    exchanges = [lines[s:e] for s, e in zip(starts, starts[1:] + [last])]
    return lines[:starts[0]], exchanges, lines[last:]


def _size(chunks):
    n = sum(len(ln) for c in chunks for ln in c) + sum(len(c) for c in chunks)
    return n - 1 if n else 0  # newline joins


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
    fact = [len(" ".join(ex[1:])) <= FACT_REPLY_CHARS and len(ex) > 1 for ex in exchanges]
    order = [n - 1] + [i for i in reversed(range(n - 1)) if fact[i]] + [i for i in reversed(range(n - 1)) if not fact[i]]
    keep = set()
    for i in order:
        trial = [header] + [exchanges[j] for j in sorted(keep | {i})] + [trailer]
        if _size([c for c in trial if c]) <= budget_chars:
            keep.add(i)
    chunks = [header] + [exchanges[j] for j in sorted(keep)] + [trailer]
    return "\n".join(ln for c in chunks for ln in c)

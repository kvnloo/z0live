"""Deterministic lane-brief compressor (C56-live-compress). No model call, no fixture markers.

compress_brief(text, budget_chars) returns a verbatim, in-order subset of the brief's lines, at most budget_chars long:

  1. a brief already within budget is returned unchanged;
  2. the brief is split into header lines (before the first "Speaker: " line), exchanges (one asker line plus every
     line up to the next asker line; the asker is the first speaker seen) and trailer lines (after the last
     exchange, e.g. a Status line);
  3. header and trailer are always kept, then exchanges are added greedily in this priority order, each only if it
     still fits: "fact" exchanges shortest first (C1-ctx-compress-r4; equal sizes newest first; was: newest
     first), except that of two or more facts filling the same slot (the asker states the same attribute of their
     own, "my <subject> is ...", same normalised subject) a newer member that carries an explicit update cue
     (CUE: now, changed, instead, no longer, ...) supersedes the older members: it is tried at the best (shortest)
     priority of itself and the members it replaces, and those are dropped (C1-ctx-compress-r9; r7 superseded on
     the slot alone, so two different true facts about one noun lost the older one; r4 tried every member by its
     own size, so a slightly shorter stale value could take the budget). Without a cue every member keeps r4's
     key; C1-ctx-compress-r11: same-slot members are further split by attribute (slot subject + predicate head,
     _attribute), so a cue statement about a different attribute of the same subject ("my kayak is now at the
     repair bay ...") supersedes nothing and keeps r4's key; C1-ctx-compress-r13: the attribute is the slot subject
     plus the first two non-filler predicate words (attribute key v2, _key), so "is booked for the ferry" and "is now
     booked in at the garage" are different attributes (r11 compared the head word alone); C1-ctx-compress-r14: when
     the predicate names the value with a label ("..., the jetty tag is X"), the attribute is the slot subject, the
     head word and that label (the 1-2 non-filler words right before the value, label tie-break, _value_label), so a
     changed location with the same label is an update and two different labels are two attributes, then filler spread
     evenly over the chat (every other filler exchange counted back from the newest older filler, newest first;
     then the skipped fillers, newest first, only if budget is left; C56-live-compress-r3, was: all filler newest first), then a filler newest exchange last
     (C56-live-compress-r4, was: the newest exchange always kept first). An exchange is
     a fact when its reply is short (<= FACT_REPLY_CHARS) or it has no reply (C1-ctx-compress-r3): answers about the user's own plans, settings, numbers and
     permissions are one line, general-knowledge explanations are paragraphs;
  4. with no speaker structure, or when header + trailer alone exceed the budget, the newest budget_chars
     characters are kept (what tiers.snapshot already does at a larger cap).

It reproduces the idea of the hand-built GH11/C56 "e5" brief (all fact pairs, header, Status line, some filler)
from any live brief. The lane snapshot supplies the character budget.
"""
import re

SPEAKER = re.compile(r"^([A-Z][\w .'-]{0,30}): ")
# C1-ctx-compress-r2: lowercase labels ("user:"/"assistant:", as context_eval and raw transcripts write them) are
# speakers too. Used only when the capitalised parse finds no speaker pair, so capitalised briefs are byte-identical.
SPEAKER_ANY_CASE = re.compile(r"^([A-Z][\w .'-]{0,30}): ", re.IGNORECASE)
CHAT_HEADER = "Recent chat context (do not read aloud):"
CHAT_SPEAKER = re.compile(r"^(user|assistant): ")
FACT_REPLY_CHARS = 200
# C1-ctx-compress-r7: a fact's slot is the subject of the asker's first own-attribute statement, "my <1-5 words>
# is/are/was/were" (case-insensitive, whitespace collapsed). Two facts with the same slot: the newer supersedes.
SLOT = re.compile(r"\bmy\s+((?:[a-z][\w'-]*\s+){0,4}?[a-z][\w'-]*)\s+(?:is|are|was|were)\b", re.IGNORECASE)
# C1-ctx-compress-r9: a same-slot member supersedes older ones only if its asker text has an explicit update cue
# (whole words, case-insensitive). Frozen in the r9 PREREG.
CUE = re.compile(r"(?<![\w-])(?:now|changed?|instead|new|no\s+longer|not\s+the\s+old|updated?|moved|switched|replaced)"
                 r"(?![\w-])", re.IGNORECASE)
# C1-ctx-compress-r11: a slot member's attribute is (slot subject, predicate head). The predicate is the asker text
# after the slot's copula; with every CUE match removed it is split into words (WORD); the head is the first word that
# is not in FILLER, or VALUE when that word holds a digit or no word is left. So "is Oak-1", "is now Oak-2 instead",
# "is updated to Oak-3" and "is no longer the old one, it is Oak-4" all have head VALUE (the value restated), while
# "is the yellow one tagged Oak-5" has head "yellow" and "is now at the repair bay ..." has head "at".
# Frozen in the r11 PREREG.
WORD = re.compile(r"[\w'-]+")
FILLER = frozenset(("a", "an", "the", "to", "it", "is", "are", "was", "were", "one", "old"))
VALUE = "<value>"
# C1-ctx-compress-r13: attribute key v2 = (slot subject, the first KEY_WORDS non-FILLER predicate words), a digit word
# ending the key as VALUE (_key). r11 used the head word alone, so "is booked for the ferry ... ref X" and "is now
# booked in at the new garage ... job card Y" shared an attribute and the cue dropped the older true fact (r12 (a)).
# FILLER, WORD, CUE and VALUE are r11's, unchanged. Frozen in the r13 PREREG.
KEY_WORDS = 2
# C1-ctx-compress-r14: label tie-break. The value's label is the last LABEL_WORDS non-FILLER words before the first
# digit word of the predicate (CUE matches removed), inside the value's clause (after the last , ; : or . before it).
# When a label exists the attribute key is (slot subject, head, LABEL, *label): "is on the north jetty, the jetty tag
# is X" and "is now on the south jetty, the jetty tag is Y" share (tender, on, LABEL, jetty, tag) (v2: north / south
# differed, the stale value stayed), while "is booked for the harbour tour, the tour ref is X" and "is now booked for
# the new repair slot, the repair ticket is Y" differ (v2: both (booked, for), the older true fact was dropped). The
# head is v2's (= r11's) first word, so r14 only splits r11 groups. No label (no digit word, or only FILLER / CUE words
# before it in its clause, e.g. "is X", "is changed to X") keeps v2's key. Frozen in the r14 PREREG.
LABEL_WORDS = 2
LABEL = "<label>"
CLAUSE = re.compile(r"[,;:.]")


def _label(line, pattern=SPEAKER):
    m = pattern.match(line)
    return m.group(1) if m else None


def _split(lines):
    if lines[0] == CHAT_HEADER:
        return _split_with(lines, CHAT_SPEAKER)
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


def _cue(exchange):
    """C1-ctx-compress-r9: True when the asker line's own text (after the speaker label) has an explicit update cue."""
    line = exchange[0]
    lab = _label(line) or _label(line, SPEAKER_ANY_CASE)
    return CUE.search(line[len(lab) + 2:] if lab else line) is not None


def _attribute(exchange):
    """C1-ctx-compress-r13: (slot subject, *key) of the asker line's first "my <subject> is ...", else None. key =
    _key(predicate after the copula), up to KEY_WORDS words (r11: the head word alone, the first non-FILLER word,
    CUE matches removed, a digit word or no word giving VALUE). C1-ctx-compress-r14: when the predicate has a value
    label (_value_label), the key is (head, LABEL, *label) instead (label tie-break; head = key[0])."""
    line = exchange[0]
    lab = _label(line) or _label(line, SPEAKER_ANY_CASE)
    text = line[len(lab) + 2:] if lab else line
    m = SLOT.search(text)
    if m is None:
        return None
    pred = text[m.end():]
    key, label = _key(pred), _value_label(pred)
    return (" ".join(m.group(1).lower().split()),) + ((key[0], LABEL) + label if label else key)


def _value_label(predicate):
    """C1-ctx-compress-r14: the value's label, the last LABEL_WORDS non-FILLER words before the first digit word of
    the predicate (CUE matches removed), only from the value's own clause; () when there is no digit word or no such
    word. "is moored at the east pontoon, the berth tag is X" -> (berth, tag); "is number X" -> (number,); "is X",
    "is changed to X" and "is X now, reception reset it" -> ()."""
    text = CUE.sub(" ", predicate)
    for m in WORD.finditer(text):
        if any(ch.isdigit() for ch in m.group(0)):
            start = max((c.end() for c in CLAUSE.finditer(text, 0, m.start())), default=0)
            words = [w.lower() for w in WORD.findall(text[start:m.start()]) if w.lower() not in FILLER]
            return tuple(words[-LABEL_WORDS:])
    return ()


def _key(predicate):
    """C1-ctx-compress-r13: attribute key v2, the first KEY_WORDS words of the predicate (CUE matches removed) that
    are not in FILLER. A word holding a digit is VALUE and ends the key (the value restated, as r11's VALUE head);
    an empty predicate gives (VALUE,). So "is booked for the ferry" -> (booked, for) and "is now booked in at the
    new garage" -> (booked, in) differ (r11: both "booked"), while "is Oak-1", "is Oak-2 now ..." and "is changed
    to Oak-3" all give (VALUE,) and "is number Oak-4" / "is now number Oak-5" both give (number, VALUE)."""
    key = []
    for w in WORD.findall(CUE.sub(" ", predicate)):
        w = w.lower()
        if any(ch.isdigit() for ch in w):
            key.append(VALUE)
            break
        if w not in FILLER:
            key.append(w)
            if len(key) == KEY_WORDS:
                break
    return tuple(key) or (VALUE,)


def _fact_order(exchanges, idx):
    """C1-ctx-compress-r11: cue-gated, attribute-aware supersession. Every fact starts with the r4 key: size
    ascending, newest first on equal sizes. Facts are grouped by attribute (_attribute: slot subject + predicate
    head). Within a group the newest member that carries an update cue (_cue) supersedes every older member of the
    group: they are dropped and it is tried at the shortest size among itself and them. Members newer than it keep
    their r4 keys. A group with no cue member (other than its oldest) is left as r4, so with no cue, or with a cue
    statement about a different attribute of the same subject, the order is exactly r4's (r9 grouped by the slot
    subject alone, so "my kayak is now at the repair bay" dropped "my kayak is the yellow one tagged X")."""
    members = {}
    for i in sorted(idx):
        k = _attribute(exchanges[i])
        if k is not None:
            members.setdefault(k, []).append(i)
    key = {i: (_size([exchanges[i]]), -i) for i in idx}
    drop = set()
    for ms in members.values():
        for pos in range(len(ms) - 1, 0, -1):
            j = ms[pos]
            if _cue(exchanges[j]):
                key[j] = (min(_size([exchanges[m]]) for m in ms[:pos + 1]), -j)
                drop.update(ms[:pos])
                break
    return sorted((i for i in idx if i not in drop), key=key.__getitem__)


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

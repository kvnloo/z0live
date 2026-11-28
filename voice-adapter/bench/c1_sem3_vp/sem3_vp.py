"""C1 sem-r3 warm route with an OPT-IN value-preserving level-3 trim (variant V1 of C1-trim-r5). Nothing in production calls it.

compress_brief_sem3_vp_info(text, budget, cache=None, judge=None, value_preserving=False) is sem-r3
(brief_compress_sem3.compress_brief_sem3_info, unchanged and vendored byte for byte from 38686df) with ONE switch:

  value_preserving=False (default): sem-r3 as is (level-3 clause trim, which can drop a leading `Name:` label or a
                                    place/name clause that sits before the digit clause).
  value_preserving=True : trim levels may drop only filler. Levels 1-2 are accepted only when every asker-line token
                          they drop is a filler word; level 3 is the asker sentence with filler phrases/words removed
                          and nothing else. If no level fits the slot, sem-r3's own "untrimmed" path keeps the line.

Evidence: z0live research C1-trim-r5 (fresh synthetic holdout, GitHub Actions run 46187039129, branch
ci/c1-trim-r5 @ d3e48e2): V1 0 new losses vs exact r4 on 112 probes (80 non-digit) in the warm and key-miss states,
warm route kept in 16/17 scenarios; unmodified sem-r3 lost 22 warm pairs; level-3-off lost 4. Synthetic cache and keys
only: no live readiness, wall-time or answer-quality claim. The patch of the module-level `trim_exchange` is guarded
by a lock and restored on exit, so concurrent calls with different flags do not interleave.

Run the check from this directory (it imports the vendored `brief_compress`, not the production module of the same name):
    python3 -m unittest -v check_sem3_vp
"""
import re
import threading

import brief_compress as bc
import brief_compress_sem as sem1
import brief_compress_sem3 as sem3

EDGE = ".,;:!?'\"()"
# Generic English discourse markers / intensifiers / politeness words. A token not listed is value-bearing and is
# never dropped (labels, names, places, colours, codewords, digit words, nouns, verbs).
FILLER_WORDS = frozenset(
    "actually basically honestly really just simply please kindly well so oh okay ok um uh er hmm anyway anyways "
    "btw fyi also again still indeed obviously literally frankly seriously definitely certainly perhaps maybe "
    "quite very pretty totally now currently sorry thanks hi hello hey oops alright".split())
FILLER_PHRASES = ("by the way", "for the record", "just so you know", "so you know", "you know", "to be honest",
                  "heads up", "i think", "i guess", "i mean", "of course", "as usual", "sort of", "kind of")
_PHRASE_RE = [re.compile(r"(?<![\w-])" + re.escape(p) + r"(?![\w-])[,;:]?[ \t]*", re.I) for p in FILLER_PHRASES]
_LOCK = threading.Lock()


def strip_filler(sentence):
    """(sentence without filler phrases/words, the set of tokens removed)."""
    s, removed = sentence, set()
    for rx in _PHRASE_RE:
        for m in rx.finditer(s):
            removed |= {t.strip(EDGE) for t in m.group(0).split()}
        s = rx.sub("", s)
    toks = s.split()
    removed |= {t.strip(EDGE) for t in toks if t.strip(EDGE).lower() in FILLER_WORDS}
    return " ".join(t for t in toks if t.strip(EDGE).lower() not in FILLER_WORDS), removed


def _tokens(text):
    return {t.strip(EDGE) for t in text.split() if t.strip(EDGE)}


def only_filler_dropped(orig_line, kept_line, removed=()):
    """True when every token of the asker line missing from the kept line is a filler word or a removed filler token."""
    return all(t.lower() in FILLER_WORDS or t in removed for t in _tokens(orig_line) - _tokens(kept_line))


def trim_levels(exchange):
    line = exchange[0]
    lab = bc._label(line) or bc._label(line, bc.SPEAKER_ANY_CASE)
    prefix = f"{lab}: " if lab else ""
    sent = sem1.statement(exchange)
    if not sent:
        return []
    body = line[len(prefix):]
    out = [(lv, ex) for lv, ex in ((1, [prefix + sent] + list(exchange[1:])), (2, [prefix + sent]))
           if only_filler_dropped(body, ex[0][len(prefix):])]
    stripped, removed = strip_filler(sent)
    if stripped and stripped != sent and sem1.HAS_VALUE.search(stripped) and only_filler_dropped(body, stripped, removed):
        out.append((3, [prefix + stripped]))
    return out


def trim_exchange(exchange, slot_chars, slack=sem3.SLACK):
    """(level, trimmed exchange) of the first value-preserving level that fits slot_chars + slack, else (None, None)."""
    for level, ex in trim_levels(exchange):
        if bc._size([ex]) <= slot_chars + slack:
            return level, ex
    return None, None


def compress_brief_sem3_vp_info(text, budget_chars, cache=None, judge=None, value_preserving=False):
    if not value_preserving:
        return sem3.compress_brief_sem3_info(text, budget_chars, cache, judge)
    with _LOCK:
        original = sem3.trim_exchange
        sem3.trim_exchange = trim_exchange
        try:
            return sem3.compress_brief_sem3_info(text, budget_chars, cache, judge)
        finally:
            sem3.trim_exchange = original


def compress_brief_sem3_vp(text, budget_chars, cache=None, judge=None, value_preserving=False):
    return compress_brief_sem3_vp_info(text, budget_chars, cache, judge, value_preserving)[0]

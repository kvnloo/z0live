"""C1-ctx-compress-r8: held-out transfer + false-merge check of the r7 supersession key (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r8_eval.py --set main --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r8_eval.py --set r8   --gguf m.gguf --blocks B,A,B,A,O --probes 0,1,2
  python ctx_compress_r8_eval.py --set r8 --dry

Harness only; no production change. Arms are the frozen r6 harness arms (ctx_compress_r6_eval.COMPRESS):
  A = bench/brief_compress_r4.py (r4 byte copy), B = brief_compress.py (production r7), O = full brief (report-only).
  --set main  the r2 suite through the frozen r6 harness (ctx_compress_r6_eval.install("main")), unchanged.
  --set r8    r8_suite(): 3 NEW scenarios (allotment garden, cycling club, community choir; the stable brief and call
              turns are the r2 scenarios' own, so prompt shape matches r6) with topic noise and, per scenario:
                (a) SAME  2 supersede pairs worded with the slot pattern ("my <subject> is" -> "my <subject> is now"),
                          subjects unlike r6's bench/badge; NEW slightly longer than OLD;
                (b) REPH  2 supersede pairs whose update does NOT match the slot pattern ("I moved ...",
                          "it changed to ...", "we switched to ..."); coverage, report-only;
                (c) FM    two different, both-true facts in one lexical slot ("my <thing> is the <colour> one, tag X",
                          later "my <thing> is at <place>, ticket Y"); probes for the OLDER fact (c_old, gated) and
                          the newer one (c_new, report-only);
                (d) UNIQ  one old unique fact, never superseded (glaze-style control).
              Every OLD / older fact is shorter than every noise exchange, every NEW / newer fact longer, so r4's
              shortest-first order reaches the old facts first and the new ones last at the 2810 margin.
              7 probes per scenario, order a1 a2 d b1 b2 c_old c_new; scored by ctx_compress_r6_eval.score_recency
              (unchanged; unique-style probes give RECALL / ABSTAIN / INVENTED).
Synthetic text only.
"""
import argparse
import dataclasses
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import tiers  # noqa: E402
from context_eval import Probe, Turn  # noqa: E402

R2_SUITE = r2.suite  # bound at import: install() replaces r2.suite with r8_suite
BUDGET = r6.BUDGET
COMPRESS = r6.COMPRESS
R8_NAMES = ["r8-allotment", "r8-cycling", "r8-choir"]
SAME_KEYS = ("a1", "a2")
REPH_KEYS = ("b1", "b2")
FM_OLD_KEYS = ("c_old",)
FM_NEW_KEYS = ("c_new",)
UNIQUE_KEYS = ("d",)
PROBE_ORDER = ("a1", "a2", "d", "b1", "b2", "c_old", "c_new")
NOISE_PAIRS = 4  # noise exchanges per scenario (8 turns)
OLD_TARGET, NEW_TARGET = 352, 382  # exchange sizes (chars); noise exchanges sit at 367
PAD = ("Thanks for keeping track of it so I can find it again quickly later on when the group gets "
       "together for the next session and the tables are all set out.").split()

TOPICS = (
    {"topic": "allotment", "noun": "Garden", "items": ("beans", "kales", "leeks", "onion", "basil", "chard", "thyme"),
     "note": " I wrote it in the plot diary by the gate, next to the watering rota, so the volunteers know.",
     "a": (("allotment plot number", "Heads up, my allotment plot number is now {}, after the reshuffle."),
           ("shed key tag", "Small change: my shed key tag is now {}, they swapped the old one.")),
     "d": "seed order reference",
     "b": (("compost bin tag", "I moved the compost bin to the far corner, so the tag on it reads {} from today."),
           ("water tap token", "About the water tap token: it changed to {} this week, the old one stopped working.")),
     "c": ("wheelbarrow", "My wheelbarrow is the green one tagged {}.",
           "My wheelbarrow is at the repair stand today, the ticket on it is {}.",
           "What is the tag code on my green wheelbarrow?", "What is the repair ticket code for my wheelbarrow?")},
    {"topic": "cycling", "noun": "Ride", "items": ("route", "climb", "flats", "coast", "ridge", "trail", "loops"),
     "note": " I wrote it on the club board by the bike racks, next to the ride sheet, so the leaders know.",
     "a": (("club jersey number", "Quick update, my club jersey number is now {}, after the new kit order."),
           ("bike rack spot", "One change: my bike rack spot is now {}, they moved the old racks.")),
     "d": "helmet sticker code",
     "b": (("saddle bag tag", "We switched to new saddle bags, so the tag on the new one says {} now."),
           ("pump locker code", "I moved the track pump to the back room, and its locker code reads {} now.")),
     "c": ("bike", "My bike is the red tourer tagged {}.",
           "My bike is booked in at the workshop this week, the job card says {}.",
           "What is the tag code on my red tourer bike?", "What is the workshop job card code for my bike?")},
    {"topic": "choir", "noun": "Choir", "items": ("hymns", "carol", "psalm", "motet", "round", "chant", "canon"),
     "note": " I wrote it on the rehearsal sheet by the piano, next to the tea urn, so the section leads know.",
     "a": (("choir folder number", "Note that my choir folder number is now {}, since the folders were redone."),
           ("rehearsal seat code", "Small update: my rehearsal seat code is now {}, they changed the rows.")),
     "d": "music stand label",
     "b": (("practice room key", "For the practice room key, it changed to {} after the locks were replaced."),
           ("car park pass", "We switched to the new car park passes, and the one I got shows {} on it.")),
     "c": ("robe", "My robe is the long blue one tagged {}.",
           "My robe is at the dry cleaner this week, the ticket for it is {}.",
           "What is the tag code on my long blue robe?", "What is the dry cleaner ticket code for my robe?")},
)


def _pair(user, reply):
    return (Turn("user", user), Turn("assistant", reply))


def _ack(label, value):
    return (f"{label} {value}, got it. I will keep that with your notes so it is easy to find later, "
            "and I can repeat it whenever you ask.")


def _fit(user, reply, target):
    """Append PAD words to the user line while the exchange stays <= target chars (deterministic)."""
    ex = [f"user: {user}", f"assistant: {reply}"]
    for w in PAD:
        trial = [ex[0] + " " + w, ex[1]]
        if brief_compress._size([trial]) > target:
            break
        ex = trial
    return _pair(ex[0][len("user: "):], reply)


def noise(t, n, start=0):
    items = t["items"]
    return tuple(Turn("user" if i % 2 == 0 else "assistant",
                      f"{t['noun']} note {i:02d}: the {items[i % len(items)]} sample has a subtle texture. "
                      "We can compare it with the other samples after the display is assembled. "
                      "No need to make a decision about that yet.") for i in range(start, start + n))


def codes(i):
    w = lambda name, base: f"{name}-{base + i}"  # noqa: E731
    return {"a1_old": w("Wren", 21), "a1_new": w("Lark", 48), "a2_old": w("Kite", 13), "a2_new": w("Dove", 64),
            "b1_old": w("Rook", 31), "b1_new": w("Heron", 57), "b2_old": w("Finch", 44), "b2_new": w("Swan", 76),
            "c_old": w("Owl", 35), "c_new": w("Crane", 62), "d": w("Gull", 82)}


def r8_facts(i):
    t, c = TOPICS[i], codes(i)
    note = t["note"]
    (a1s, a1u), (a2s, a2u) = t["a"]
    (b1s, b1u), (b2s, b2u) = t["b"]
    cs, c_old_txt, c_new_txt, c_old_q, c_new_q = t["c"]
    old_facts = (
        _fit(f"My {a1s} is {c['a1_old']}." + note, _ack(a1s.capitalize(), c["a1_old"]), OLD_TARGET),
        _fit(c_old_txt.format(c["c_old"]) + note, _ack(cs.capitalize() + " tag", c["c_old"]), OLD_TARGET),
        _fit(f"My {b1s} is {c['b1_old']}." + note, _ack(b1s.capitalize(), c["b1_old"]), OLD_TARGET),
        _fit(f"My {a2s} is {c['a2_old']}." + note, _ack(a2s.capitalize(), c["a2_old"]), OLD_TARGET),
        _fit(f"My {t['d']} is {c['d']}." + note, _ack(t["d"].capitalize(), c["d"]), OLD_TARGET),
        _fit(f"My {b2s} is {c['b2_old']}." + note, _ack(b2s.capitalize(), c["b2_old"]), OLD_TARGET))
    new_facts = (
        _fit(c_new_txt.format(c["c_new"]) + note, _ack(cs.capitalize() + " ticket", c["c_new"]), NEW_TARGET),
        _fit(b1u.format(c["b1_new"]) + note, _ack(b1s.capitalize(), c["b1_new"]), NEW_TARGET),
        _fit(b2u.format(c["b2_new"]) + note, _ack(b2s.capitalize(), c["b2_new"]), NEW_TARGET),
        _fit(a1u.format(c["a1_new"]) + note, _ack(a1s.capitalize(), c["a1_new"]), NEW_TARGET),
        _fit(a2u.format(c["a2_new"]) + note, _ack(a2s.capitalize(), c["a2_new"]), NEW_TARGET))
    code = " Please give just the code."
    by_key = {
        "a1": Probe("a1", f"What is my {a1s} now?" + code, c["a1_new"], (c["a1_old"],)),
        "a2": Probe("a2", f"What is my {a2s} now?" + code, c["a2_new"], (c["a2_old"],)),
        "d": Probe("d", f"What is my {t['d']}?" + code, c["d"]),
        "b1": Probe("b1", f"What is my {b1s} now?" + code, c["b1_new"], (c["b1_old"],)),
        "b2": Probe("b2", f"What is my {b2s} now?" + code, c["b2_new"], (c["b2_old"],)),
        "c_old": Probe("c_old", c_old_q + code, c["c_old"]),
        "c_new": Probe("c_new", c_new_q + code, c["c_new"])}
    return sum(old_facts, ()), sum(new_facts, ()), tuple(by_key[k] for k in PROBE_ORDER)


def r8_suite():
    out = []
    for i, s in enumerate(R2_SUITE()):
        old, new, probes = r8_facts(i)
        t = TOPICS[i]
        chat = old + noise(t, 2 * NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=R8_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


def predict(cur_in, old_in):
    """Expected class if Gemma answers from what the context holds (dry prediction, not a score)."""
    if cur_in and old_in:
        return "BOTH"
    if cur_in:
        return "CURRENT"
    if old_in:
        return "STALE"
    return "ABSENT"


def dry(scenarios):
    out = []
    for s in scenarios:
        brief = r2.chat_brief(s)
        row = {"scenario": s.name, "brief_chars": len(brief)}
        for arm in COMPRESS:  # r8 arms directly (not r2.COMPRESS), so the dry result never depends on install()
            ctx = tiers.snapshot(COMPRESS[arm](brief), context_eval.exchange(s.call))
            row[arm] = {"brief_chars": len(COMPRESS[arm](brief)),
                        "system_chars": len(tiers.SYSTEM.format(tier="fast") + "\n\n" + ctx),
                        "items": {p.key: predict(p.current in ctx, any(v in ctx for v in p.superseded))
                                  for p in s.probes}}
        out.append(row)
    return out


def install(which):
    if which == "main":
        r6.install("main")
        return
    r2.COMPRESS = COMPRESS
    r2.dry = dry
    r2.suite = r8_suite
    r2.context_eval.score = r6.score_recency


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("main", "r8"), required=True)
    o, rest = ap.parse_known_args(argv)
    install(o.set)
    if "--meta" in rest:
        k = rest.index("--meta") + 1
        rest[k] = json.dumps({**json.loads(rest[k]), "r8_set": o.set, "r6_set": o.set if o.set == "main" else "r8",
                              "r8_harness": "ctx-compress-r8"})
    sys.argv = [str(HERE / "ctx_compress_r2_eval.py")] + rest
    return r2.main()


if __name__ == "__main__":
    raise SystemExit(main())

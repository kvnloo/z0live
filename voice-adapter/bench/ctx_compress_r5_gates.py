"""C1-ctx-compress-r5 frozen gate check (rule in the r5 PREREG; never edited after scoring).

  python ctx_compress_r5_gates.py verdict.json job*.json > gates.json

Outcome = r2 frozen verdict (ctx_compress_r2_verdict.py, unchanged) with gates on top. Blocks are matched by label
(A1 A2 B1 B2), so any block order works. Counts are summed over jobs.

Planted probes (probe.current set):
  for each of SKIP, SUPERSEDED, INVENTED, each B block count must be <= max(A1, A2) count.
Unplanted probes (probe.current is None, the right answer is "I don't know"):
  an answer is an ABSTAIN if it is SKIP or an "I do not have / don't know" style abstention, else WRONG
  (a non-abstaining answer: INVENTED or OFF_FORMAT that is not an abstention). Gates: each B block WRONG count
  <= max(A1, A2) WRONG count, and each B block INVENTED count <= max(A1, A2) INVENTED count.
  SKIP vs abstention on unplanted probes is equivalent (both decline) and is report-only.
A failed gate turns KEEP into REVERT; NULL and REVERT pass through unchanged.
"""
import json
import re
import sys

PLANTED_GATES = ("SKIP", "SUPERSEDED", "INVENTED")
UNPLANTED_GATES = ("WRONG", "INVENTED")
PRIMARY = ("A1", "B1", "A2", "B2")
ABSTAIN = re.compile(
    r"\bI (?:do not|don't|dont|do n't) (?:have|know)\b"
    r"|\bI(?: am|'m) not (?:sure|certain|aware)\b"
    r"|\bI (?:can ?not|can't|cannot) find\b"
    r"|\b(?:not|isn't|is not) (?:in|mentioned in|listed in) (?:the|my|your|this) (?:context|notes|brief|chat|call)\b"
    r"|\bno (?:record|mention) of\b",
    re.IGNORECASE)


def abstains(row):
    return row["result"] == "SKIP" or bool(ABSTAIN.search(row.get("reply") or ""))


def gates(verdict, jobs):
    planted = {lab: {g: 0 for g in PLANTED_GATES} for lab in PRIMARY}
    unplanted = {lab: {"n": 0, "SKIP": 0, "ABSTAIN_TEXT": 0, "ABSTAIN": 0, "WRONG": 0, "INVENTED": 0}
                 for lab in PRIMARY}
    for j in jobs:
        for b in j.get("blocks", []):
            lab = b["label"]
            if lab not in planted:
                continue
            for r in b["rows"]:
                res = r["result"]
                if r["current"] is not None:
                    if res in PLANTED_GATES:
                        planted[lab][res] += 1
                    continue
                u = unplanted[lab]
                u["n"] += 1
                if abstains(r):
                    u["ABSTAIN"] += 1
                    u["SKIP" if res == "SKIP" else "ABSTAIN_TEXT"] += 1
                elif res != "ERROR":
                    u["WRONG"] += 1
                    if res == "INVENTED":
                        u["INVENTED"] += 1
    failed = []
    for scope, cnt, names in (("planted", planted, PLANTED_GATES), ("unplanted", unplanted, UNPLANTED_GATES)):
        for g in names:
            amax = max(cnt["A1"][g], cnt["A2"][g])
            failed += [f"{scope} {g} {lab} {cnt[lab][g]} > A max {amax}" for lab in ("B1", "B2") if cnt[lab][g] > amax]
    outcome = verdict["outcome"]
    if outcome == "KEEP" and failed:
        outcome = "REVERT"
    return {"outcome": outcome, "verdict_outcome": verdict["outcome"], "planted_counts": planted,
            "unplanted_counts": unplanted, "gate_failures": failed}


if __name__ == "__main__":
    v = json.load(open(sys.argv[1]))
    print(json.dumps(gates(v, [json.load(open(p)) for p in sys.argv[2:]]), indent=1))

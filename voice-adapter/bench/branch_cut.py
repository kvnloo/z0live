"""Offline test for speculative branching (docs/SPECULATIVE-BRANCHING.md, attack item 2).

For each question: answer the full text and a version cut early, with the same fast lane model and the same lane
prompt as a live call; a grader model says whether the early answer gives the owner the same substantive response.
Hit rate = how often answering before he finished would have been right.

Cuts are word-fraction cuts (70% and 85% of the words); real transcripts have no pause timestamps here, so this
approximates "cut at the last pause". SKIP vs SKIP counts as a match (both defer to Hermes).
  .venv/bin/python bench/branch_cut.py QUESTIONS.json [--fast openai-codex:gpt-6-luna] [--n 0] -> JSON summary
Uses the running lane server (127.0.0.1:8646); no live call is touched.
"""
import argparse
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tiers  # noqa: E402  (the live lane prompt)

URL = "http://127.0.0.1:8646/v1/ask"
GRADE = ("Two assistant replies to the same voice-call user. Reply A answered only the first part of the user's words "
         "(cut off early); reply B answered the full words. Does A give the user the same substantive response as B "
         "(same facts/decision, or both defer/skip)? Answer exactly MATCH or MISS.\n\nFull user words: {q}\n\nA: {a}\n\nB: {b}")


def ask(provider, model, system, user):
    body = json.dumps({"provider": provider, "model": model, "system": system, "user": user}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.loads(r.read())
    return (out.get("text") or out.get("answer") or "").strip()


def cut(q, frac):
    w = q.split()
    return " ".join(w[: max(3, int(len(w) * frac))])


def one(q, fast, grader):
    fp, fm = fast.split(":", 1)
    gp, gm = grader.split(":", 1)
    try:
        full = ask(fp, fm, tiers.SYSTEM, q)
        row = {"q": q, "full": full}
        for frac in (0.7, 0.85):
            part = ask(fp, fm, tiers.SYSTEM, cut(q, frac))
            both_skip = "SKIP" in full and "SKIP" in part
            verdict = "MATCH" if both_skip else ask(gp, gm, "You are a strict grader.", GRADE.format(q=q, a=part, b=full))
            row[str(frac)] = {"answer": part, "hit": verdict.upper().startswith("MATCH"), "both_skip": both_skip}
        return row
    except Exception as exc:  # noqa: BLE001 - counted, not fatal
        return {"q": q, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("questions")
    ap.add_argument("--fast", default="openai-codex:gpt-6-luna")
    ap.add_argument("--grader", default="claude-subscription-directsdk-experimental:claude-sonnet-5-5")
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    qs = json.loads(Path(a.questions).read_text())
    qs = qs[: a.n] if a.n else qs
    with ThreadPoolExecutor(6) as pool:
        rows = list(pool.map(lambda q: one(q, a.fast, a.grader), qs))
    ok = [r for r in rows if "error" not in r]
    summ = {"questions": len(qs), "errors": len(rows) - len(ok), "fast": a.fast, "grader": a.grader}
    for frac in ("0.7", "0.85"):
        hits = [r[frac]["hit"] for r in ok]
        real = [r[frac]["hit"] for r in ok if not r[frac]["both_skip"]]
        summ[f"cut_{frac}"] = {"hit_rate": round(sum(hits) / len(hits), 3) if hits else None,
                               "hit_rate_excluding_both_skip": round(sum(real) / len(real), 3) if real else None,
                               "both_skip": sum(r[frac]["both_skip"] for r in ok)}
    if a.out:
        Path(a.out).write_text(json.dumps({"summary": summ, "rows": rows}, indent=1))
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()

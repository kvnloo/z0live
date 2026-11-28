"""C1-ctx-sem-r5 JUDGE ORACLE (not scored): replay the collision judge on every pair sem-r3 judged (run 46105850056)
plus the sem-r4 oracle's new pairs, with the sem-r2 prompt (old, twice: A/A control + repeat) and the changed prompt
(JUDGE_PROMPT_R5), one llama-server, same gguf, greedy temp 0 seed 42, max 4 tokens (sem2._post).

  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r5_oracle.py --gguf m.gguf --out oracle.json
  python ctx_compress_sem_r5_oracle.py --summarize oracle.json        (no model: recompute the summary from the rows)

Counts and stop rule are frozen in ctx-compress-sem-r5-oracle-PREREG.md. Synthetic text only.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress_sem2 as sem2  # noqa: E402
import brief_compress_sem5 as sem5  # noqa: E402

PAIRS = HERE / "ctx_compress_sem_r5_pairs.json"
PROMPTS = {"old1": sem2.JUDGE_PROMPT, "new": sem5.JUDGE_PROMPT_R5, "old2": sem2.JUDGE_PROMPT}
ORDER = ("old1", "new", "old2")


def judge_one(url, prompt, older, newer):
    reply = sem2._post(url, prompt.format(old=sem2.norm_statement(older), new=sem2.norm_statement(newer)), 4, 120.0, None)
    return {"reply": reply, "verdict": sem2.parse_judge(reply)}


def summarize(pairs, rows):
    """pairs: list of pair dicts; rows: {id: {"old1": {...}, "new": {...}, "old2": {...}}}. Pure function."""
    out = {"n_pairs": len(pairs), "n_extra": sum(bool(p.get("extra")) for p in pairs)}
    v = lambda p, a: (rows[p["id"]][a]["verdict"] if p["id"] in rows else None)  # noqa: E731
    clear = [p for p in pairs if p["gold"] is not None and not p["ambiguous"]]
    rec = [p for p in pairs if not p.get("extra")]
    out["aa_flips_vs_recorded"] = [p["id"] for p in rec if v(p, "old1") != p["sem3"]]
    out["aa_flips_repeat"] = [p["id"] for p in pairs if v(p, "old1") != v(p, "old2")]
    ups = [p for p in clear if p["gold"] == "replace"]
    sts = [p for p in clear if p["gold"] == "also"]
    out["fixed"] = [p["id"] for p in ups if p["sem3"] == "also" and v(p, "new") == "replace"]
    out["clear_false_also_sem3"] = [p["id"] for p in ups if p["sem3"] == "also"]
    out["kept"] = [p["id"] for p in ups if p["sem3"] == "replace" and v(p, "new") == "replace"]
    out["lost"] = [p["id"] for p in ups if p["sem3"] == "replace" and v(p, "new") != "replace"]
    out["new_false_replace"] = [p["id"] for p in sts if v(p, "new") == "replace" and v(p, "old1") != "replace"]
    out["false_replace_new_all"] = [p["id"] for p in sts if v(p, "new") == "replace"]
    out["false_replace_old_all"] = [p["id"] for p in sts if v(p, "old1") == "replace"]
    out["still_true_n"] = len(sts)
    out["update_n"] = len(ups)
    out["noise_replace_new"] = [p["id"] for p in pairs if p["class"] == "noise" and v(p, "new") == "replace"]
    out["noise_replace_old"] = [p["id"] for p in pairs if p["class"] == "noise" and v(p, "old1") == "replace"]
    out["ambiguous"] = [{"id": p["id"], "old": v(p, "old1"), "new": v(p, "new"), "sem3": p["sem3"]}
                        for p in pairs if p["ambiguous"]]
    out["unparsable_new"] = [p["id"] for p in pairs if v(p, "new") is None]
    out["by_set_new_false_replace"] = {}
    for i in out["new_false_replace"]:
        s = next(p["set"] for p in pairs if p["id"] == i)
        out["by_set_new_false_replace"][s] = out["by_set_new_false_replace"].get(s, 0) + 1
    aa = len(out["aa_flips_vs_recorded"]) + len(out["aa_flips_repeat"])
    out["aa_flips_total"] = aa
    ok = len(out["fixed"]) >= 1 and len(out["new_false_replace"]) == 0 and aa == 0
    out["proceed_to_scored_run"] = bool(ok)
    out["outcome"] = "PASS(oracle)" if ok else "NULL(oracle-stop)"
    out["stop_rule"] = "proceed iff fixed >= 1 AND new_false_replace == 0 AND aa_flips_total == 0"
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    data = json.loads(PAIRS.read_text(encoding="utf-8"))
    pairs = data["pairs"]
    if argv[:1] == ["--summarize"]:
        d = json.loads(Path(argv[1]).read_text())
        rows = {int(k): v for k, v in d["rows"].items()}
        print(json.dumps(summarize(pairs, rows), indent=1))
        return 0
    gguf = argv[argv.index("--gguf") + 1]
    out_path = argv[argv.index("--out") + 1]
    server = os.environ["LLAMA_SERVER"]
    import lanespeed
    url = f"http://127.0.0.1:{lanespeed.PORT}"
    log = open(out_path + ".server.log", "w")
    proc, cmd = lanespeed.serve(server, gguf, 512, log)
    rows, t0 = {}, time.monotonic()
    try:
        try:
            lanespeed.ask("You are a helpful assistant.", "Say ok.")   # warm-up, not scored
        except Exception:  # noqa: BLE001
            pass
        for arm in ORDER:
            for p in pairs:
                rows.setdefault(p["id"], {})[arm] = judge_one(url, PROMPTS[arm], p["older"], p["newer"])
    finally:
        lanespeed.stop(proc)
    res = {"harness": "ctx-compress-sem-r5-oracle", "cmd": cmd, "wall_s": round(time.monotonic() - t0, 1),
           "meta": json.loads(argv[argv.index("--meta") + 1]) if "--meta" in argv else {},
           "rows": rows, "summary": summarize(pairs, rows)}
    Path(out_path).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

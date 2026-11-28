"""C1-ctx-sem-r7 verdict (independent of the oracle's summarize): python ctx_compress_sem_r7_verdict.py oracle.json
Prints the stop-rule quantities and the outcome. Used locally on the downloaded artifact and in CI (cross-check)."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def verdict(oracle_path):
    pairs = json.loads((HERE / "ctx_compress_sem_r5_pairs.json").read_text())["pairs"]
    base = {int(k): x for k, x in json.loads((HERE / "ctx_compress_sem_r7_e2b_old.json").read_text())["verdicts"].items()}
    rows = {int(k): r for k, r in json.loads(Path(oracle_path).read_text())["rows"].items()}
    new = {i: r["p1"]["verdict"] for i, r in rows.items()}
    clear_up = [p for p in pairs if p["gold"] == "replace" and not p["ambiguous"]]
    clear_st = [p for p in pairs if p["gold"] == "also" and not p["ambiguous"]]
    noise = [p for p in pairs if p["class"] == "noise"]
    blind = ("r15", "v2")
    fixed = [p["id"] for p in clear_up if p["set"] in blind and p["sem3"] == "also" and new[p["id"]] == "replace"]
    nfr = [p["id"] for p in clear_st if p["set"] in blind and new[p["id"]] == "replace" and base[p["id"]] != "replace"]
    noise_new = [p["id"] for p in noise if new[p["id"]] == "replace"]
    noise_old = [p["id"] for p in noise if base[p["id"]] == "replace"]
    flips = [i for i, r in rows.items() if r["p1"]["verdict"] != r["p2"]["verdict"]]
    ok = len(fixed) >= 1 and not nfr and len(noise_new) <= len(noise_old) and not flips
    return {"fixed": fixed, "new_false_replace_blind": nfr, "noise_replace_e4b": noise_new, "noise_replace_e2b": noise_old,
            "aa_verdict_flips": flips, "updates_replace_e4b": sum(new[p["id"]] == "replace" for p in clear_up),
            "still_true_replace_e4b": sum(new[p["id"]] == "replace" for p in clear_st),
            "outcome": "PASS(oracle)" if ok else "NULL(oracle-stop)"}


if __name__ == "__main__":
    print(json.dumps(verdict(sys.argv[1]), indent=1))

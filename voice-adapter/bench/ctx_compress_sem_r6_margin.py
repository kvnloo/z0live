"""C1-ctx-sem-r6 CALIBRATED-JUDGE ORACLE (not scored). Same sem-r2 JUDGE_PROMPT, same model/flags as sem-r5; the ONLY change
is the decision rule: margin = ln P(first token in REPLACE variants) - ln P(first token in ALSO variants), REPLACE iff
margin > t, t fitted on DEV pairs only (rule frozen in ctx-compress-sem-r6-oracle-PREREG.md).

  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r6_margin.py --gguf m.gguf --out margins.json
  python ctx_compress_sem_r6_margin.py --summarize margins.json     (no model: recompute the summary from the rows)

Synthetic text only.
"""
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress_sem2 as sem2  # noqa: E402

PAIRS = HERE / "ctx_compress_sem_r5_pairs.json"          # the r5 file, unchanged (same sha256)
DEV_SETS = ("main", "recency", "r8", "r12", "r13", "r14")
BLIND_SETS = ("r15", "v2")
REPLACE_VARIANTS = ("REPLACE", " REPLACE", "Replace", " Replace", "replace", " replace")
ALSO_VARIANTS = ("ALSO", " ALSO", "Also", " Also", "also", " also")
TOP_N = 50
FLOOR = 1e-12     # probability floor for a variant group absent from the top-N list (margin clipped to +-27.6)


def _json(url, body, timeout=120.0):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def first_token_sets(url):
    """Tokenise every variant with the server's own tokenizer; return (info, replace_strings, also_strings).
    The first token of each variant is detokenised to its string; the two groups must be disjoint."""
    info = {"replace": {}, "also": {}}
    groups = {"replace": set(), "also": set()}
    for name, variants in (("replace", REPLACE_VARIANTS), ("also", ALSO_VARIANTS)):
        for v in variants:
            ids = _json(url + "/tokenize", {"content": v, "add_special": False})["tokens"]
            first = _json(url + "/detokenize", {"tokens": [ids[0]]})["content"]
            info[name][v] = {"ids": ids, "first_id": ids[0], "first_str": first,
                             "pieces": [_json(url + "/detokenize", {"tokens": [i]})["content"] for i in ids]}
            groups[name].add(first)
    if groups["replace"] & groups["also"]:
        raise RuntimeError("REPLACE and ALSO share a first token: " + repr(groups["replace"] & groups["also"]))
    return info, groups["replace"], groups["also"]


def margin_of(top, rset, aset):
    """top: [{'token': str, 'logprob': float}, ...] first generated position. Sums probability over each variant group
    (distinct token strings only). Returns (margin, p_replace, p_also)."""
    seen, pr, pa = set(), 0.0, 0.0
    for t in top:
        tok = t["token"]
        if tok in seen:
            continue
        seen.add(tok)
        if tok in rset:
            pr += math.exp(t["logprob"])
        elif tok in aset:
            pa += math.exp(t["logprob"])
    return math.log(max(pr, FLOOR)) - math.log(max(pa, FLOOR)), pr, pa


def judge_one(url, older, newer, rset, aset):
    content = sem2.JUDGE_PROMPT.format(old=sem2.norm_statement(older), new=sem2.norm_statement(newer))
    body = {"messages": [{"role": "user", "content": content}], "stream": False, "temperature": 0, "seed": 42,
            "max_tokens": 4, "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": False,
            "logprobs": True, "top_logprobs": TOP_N}
    data = _json(url + "/v1/chat/completions", body)
    ch = data["choices"][0]
    reply = ch["message"]["content"]
    lp = ((ch.get("logprobs") or {}).get("content") or [])
    top = lp[0]["top_logprobs"] if lp else []
    m, pr, pa = margin_of(top, rset, aset)
    return {"reply": reply, "verdict": sem2.parse_judge(reply), "margin": m, "p_replace": pr, "p_also": pa,
            "first": lp[0]["token"] if lp else None, "top1": top[0]["token"] if top else None, "n_top": len(top)}


def auc(pos, neg):
    """P(margin_pos > margin_neg) + 0.5 P(tie)."""
    if not pos or not neg:
        return None
    s = sum((1.0 if p > n else 0.5 if p == n else 0.0) for p in pos for n in neg)
    return s / (len(pos) * len(neg))


def fit_threshold(dev):
    """dev: list of (margin, role) with role 'pos' (clear update) or 'neg' (clear still-true or noise).
    t = midpoint between m_neg (the highest dev negative margin) and u (the lowest dev update margin strictly above
    m_neg): the single gap that keeps every dev negative ALSO and keeps the most dev updates REPLACE. Rule is REPLACE iff
    margin > t. Dev updates with margin <= m_neg cannot be separated and are counted lost. No update above m_neg:
    t = m_neg + 1.0."""
    negs = [m for m, r in dev if r == "neg"]
    pos = [m for m, r in dev if r == "pos"]
    m_neg = max(negs)
    above = [m for m in pos if m > m_neg]
    if above:
        u = min(above)
        t = (m_neg + u) / 2.0
    else:
        u, t = None, m_neg + 1.0
    return {"t": t, "m_neg_max": m_neg, "u_min_pos_above": u, "dev_pos_n": len(pos), "dev_pos_replace": len(above),
            "dev_pos_lost": len(pos) - len(above), "dev_neg_n": len(negs)}


def summarize(pairs, rows):
    """rows: {id: {'p1': {...}, 'p2': {...}}}. Pure function; decisions use pass 1, pass 2 is the A/A control."""
    mg = lambda p, a: rows[p["id"]][a]["margin"]  # noqa: E731
    clear = lambda p: p["gold"] is not None and not p["ambiguous"]  # noqa: E731
    dev = [p for p in pairs if p["set"] in DEV_SETS]
    blind = [p for p in pairs if p["set"] in BLIND_SETS]
    role = lambda p: ("pos" if p["gold"] == "replace" else "neg") if clear(p) else ("neg" if p["class"] == "noise" else None)  # noqa: E731
    fit = fit_threshold([(mg(p, "p1"), role(p)) for p in dev if role(p)])
    t = fit["t"]
    dec = lambda p, a: "replace" if mg(p, a) > t else "also"  # noqa: E731
    old = lambda p: rows[p["id"]]["p1"]["verdict"]  # noqa: E731
    out = {"n_pairs": len(pairs), "n_dev": len(dev), "n_blind": len(blind), "fit": fit, "t": t, "dev_sets": list(DEV_SETS),
           "blind_sets": list(BLIND_SETS)}
    ups = [p for p in blind if clear(p) and p["gold"] == "replace"]
    sts = [p for p in blind if clear(p) and p["gold"] == "also"]
    out["blind_update_n"], out["blind_still_true_n"] = len(ups), len(sts)
    out["blind_clear_false_also_old"] = [p["id"] for p in ups if p["sem3"] == "also"]
    out["fixed"] = [p["id"] for p in ups if p["sem3"] == "also" and dec(p, "p1") == "replace"]
    out["blind_kept"] = [p["id"] for p in ups if p["sem3"] == "replace" and dec(p, "p1") == "replace"]
    out["blind_lost"] = [p["id"] for p in ups if p["sem3"] == "replace" and dec(p, "p1") != "replace"]
    out["new_false_replace"] = [p["id"] for p in sts if dec(p, "p1") == "replace" and old(p) != "replace"]
    out["false_replace_cal_blind_all"] = [p["id"] for p in sts if dec(p, "p1") == "replace"]
    out["false_replace_old_blind_all"] = [p["id"] for p in sts if old(p) == "replace"]
    cl_st_all = [p for p in pairs if clear(p) and p["gold"] == "also"]
    out["new_false_replace_all_sets"] = [p["id"] for p in cl_st_all if dec(p, "p1") == "replace" and old(p) != "replace"]
    out["still_true_all"] = [{"id": p["id"], "set": p["set"], "scenario": p["scenario"], "margin": mg(p, "p1"),
                              "cal": dec(p, "p1"), "old": old(p)} for p in cl_st_all]
    noise = [p for p in pairs if p["class"] == "noise"]
    out["noise_n"] = len(noise)
    out["noise_replace_cal"] = [p["id"] for p in noise if dec(p, "p1") == "replace"]
    out["noise_replace_old"] = [p["id"] for p in noise if old(p) == "replace"]
    out["noise_replace_cal_blind"] = [p["id"] for p in noise if p["set"] in BLIND_SETS and dec(p, "p1") == "replace"]
    out["noise_max_margin"] = max(mg(p, "p1") for p in noise)
    all_ups = [p for p in pairs if clear(p) and p["gold"] == "replace"]
    out["all_update_n"], out["all_still_true_n"] = len(all_ups), len(cl_st_all)
    out["auc_update_vs_still_true"] = auc([mg(p, "p1") for p in all_ups], [mg(p, "p1") for p in cl_st_all])
    out["auc_update_vs_still_true_dev"] = auc([mg(p, "p1") for p in all_ups if p["set"] in DEV_SETS],
                                              [mg(p, "p1") for p in cl_st_all if p["set"] in DEV_SETS])
    out["auc_update_vs_noise"] = auc([mg(p, "p1") for p in all_ups], [mg(p, "p1") for p in noise])
    out["cal_replace_clear_updates_all"] = sum(dec(p, "p1") == "replace" for p in all_ups)
    out["old_replace_clear_updates_all"] = sum(old(p) == "replace" for p in all_ups)
    out["targets"] = [{"id": p["id"], "set": p["set"], "scenario": p["scenario"], "margin": mg(p, "p1"),
                       "cal": dec(p, "p1"), "old": old(p)} for p in ups if p["sem3"] == "also"]
    out["ambiguous"] = [{"id": p["id"], "scenario": p["scenario"], "margin": mg(p, "p1"), "cal": dec(p, "p1"),
                         "old": old(p)} for p in pairs if p["ambiguous"]]
    out["aa_decision_flips"] = [p["id"] for p in pairs if dec(p, "p1") != dec(p, "p2")]
    out["aa_verdict_flips"] = [p["id"] for p in pairs if rows[p["id"]]["p1"]["verdict"] != rows[p["id"]]["p2"]["verdict"]]
    out["aa_max_abs_margin_diff"] = max(abs(mg(p, "p1") - mg(p, "p2")) for p in pairs)
    out["old_vs_recorded_flips"] = [p["id"] for p in pairs if not p.get("extra") and old(p) != p["sem3"]]
    out["sign_vs_reply_disagree"] = [p["id"] for p in pairs if (mg(p, "p1") > 0) != (old(p) == "replace")]
    out["top1_not_first_generated"] = [p["id"] for p in pairs if rows[p["id"]]["p1"]["top1"] != rows[p["id"]]["p1"]["first"]]
    out["unparsable"] = [p["id"] for p in pairs if old(p) is None]
    ok = (len(out["fixed"]) >= 1 and not out["new_false_replace"]
          and len(out["noise_replace_cal"]) <= len(out["noise_replace_old"]) and not out["aa_decision_flips"])
    out["proceed_to_scored_run"] = bool(ok)
    out["outcome"] = "PASS(oracle)" if ok else "NULL(oracle-stop)"
    out["stop_rule"] = ("PASS iff blind fixed >= 1 AND blind new_false_replace == 0 AND noise REPLACE (all pairs) not above the "
                        "old-prompt count AND A/A decision flips == 0")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))["pairs"]
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
    rows, t0, tok = {}, time.monotonic(), None
    try:
        try:
            lanespeed.ask("You are a helpful assistant.", "Say ok.")   # warm-up, not scored
        except Exception:  # noqa: BLE001
            pass
        tok, rset, aset = first_token_sets(url)
        for arm in ("p1", "p2"):
            for p in pairs:
                rows.setdefault(p["id"], {})[arm] = judge_one(url, p["older"], p["newer"], rset, aset)
    finally:
        lanespeed.stop(proc)
    res = {"harness": "ctx-compress-sem-r6-oracle", "cmd": cmd, "wall_s": round(time.monotonic() - t0, 1),
           "meta": json.loads(argv[argv.index("--meta") + 1]) if "--meta" in argv else {},
           "tokenization": tok, "replace_first_strs": sorted(rset), "also_first_strs": sorted(aset),
           "rows": rows, "summary": summarize(pairs, rows)}
    Path(out_path).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

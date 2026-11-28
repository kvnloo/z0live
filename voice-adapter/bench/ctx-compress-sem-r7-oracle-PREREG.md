# C1-ctx-sem-r7 judge-capacity oracle: PREREG (written before any model call of this experiment)

Not a scored run. Question: does a LARGER collision judge (gemma-4-E4B-it Q4_K_M) with the UNCHANGED sem-r2 `JUDGE_PROMPT` and the
raw reply parse fix the 2 clear blind false-ALSO verdicts of sem-r3 (v2-gym Tango-303/311, v2-pickup Route-12/15) without a new
false-REPLACE on a clear still-true pair?

## Lessons carried in
- r5 (prompt rewrite): fixed 2 but pushed nearly everything toward REPLACE (7 new false-REPLACE) -> NULL. Prompt wording is not touched here.
- r6 (logprob margin calibration): errors of the E2B judge are confident (still-true r12 coworking margin +13.6 REPLACE; targets -6.6 / -14.0 ALSO),
  so a threshold cannot separate them; fixed 0 -> NULL. No threshold or calibration is used here: decision = argmax reply, parsed as before.
- What this design changes: only judge capacity (E2B -> E4B, ~2x effective parameters). Prompt, parse, decoding, server flags, pairs and stop
  rule shape are identical, so any difference is attributable to the model. If E4B is equally confident and wrong, the result is NULL.

## The one change
Model: `unsloth/gemma-4-E4B-it-GGUF` @ revision `bfc15c382204943c3a8fff0c750b94ae2364d7a3`, file `gemma-4-E4B-it-Q4_K_M.gguf`
(4,977,171,584 bytes), sha256 / Hub lfs.oid `85a896a047553e842f25297ee5b031d64ff30147d9c4af17b1e4b394cd1fab87` (read from
https://huggingface.co/api/models/unsloth/gemma-4-E4B-it-GGUF/tree/main and the same tree at the pinned revision; CI downloads from the
pinned revision and fails unless `sha256sum` equals it; the computed sha256 is also written into the artifact).
Everything else as sem-r5 old-prompt arm: sem-r2 `JUDGE_PROMPT` (`brief_compress_sem2.py`, sha in FREEZE), `sem2._post` (temp 0, seed 42,
max_tokens 4, enable_thinking false, cache_prompt false), `sem2.parse_judge` on the reply, llama.cpp b12527 prebuilt, ubuntu-24.04-arm CPU,
`lanespeed.serve` flags (`-c 8192 -np 1 --cache-ram 0 --jinja --no-webui -ub 512`), one unscored warm-up, then two passes (p1, p2) over all
198 pairs in file order. Fallback if two passes cannot finish in the job cap: deterministic split of pairs across matrix jobs (index mod n) with
the same server flags, NOT a different model (job timeout is set to 170 min; expected ~25 min).

## Pairs (frozen)
`ctx_compress_sem_r5_pairs.json` (sha256 7413882b...), 198 pairs. Gold: 65 clear update, 9 clear still-true, 2 ambiguous (report-only),
122 noise. DEV = main, recency, r8, r12, r13, r14; BLIND = r15, v2. Baseline = E2B old-prompt verdicts on all 198 pairs
(`ctx_compress_sem_r7_e2b_old.json`, from the sem-r5 oracle old1; equal to old2 and to sem-r6 p1 on every pair; 63/65 updates REPLACE,
1/9 still-true REPLACE (r12 coworking), 1/122 noise REPLACE (id 163)).

## Stop rule (frozen, same shape as r5/r6)
PASS(oracle) iff ALL of:
1. blind clear false-ALSO fixed >= 1 of 2 (clear update pair, set r15 or v2, sem-r3 verdict ALSO, E4B p1 verdict REPLACE);
2. new false-REPLACE == 0: no blind clear still-true pair that E4B p1 calls REPLACE while the E2B old reply was not REPLACE;
3. noise REPLACE over the 122 noise pairs <= E2B old noise REPLACE count (1);
4. A/A: E4B p1 vs p2 verdict flips == 0 over all 198 pairs.
Otherwise NULL(oracle-stop): no scored run, no PR. PASS -> a DRAFT PR to staging for a later scored run (not part of this task's oracle).
Verdict is computed in CI (oracle summary cross-checked by an independent script `ctx_compress_sem_r7_verdict.py`) and again locally on the
downloaded artifact; they must match.

## Reported, not gating
Per-set REPLACE counts (E4B vs E2B); updates REPLACE x/65 (E2B 63); still-true REPLACE x/9 (E2B 1); dev new false-REPLACE; ambiguous
pairs; E4B changes vs E2B on any pair; per-request wall time p50/p95 on the GHA arm CPU (E2B ~1.5 s p50) and whether a later scored run
fits the per-statement latency budget (judged statement = one judge request per colliding pair). Synthetic text only.

# C1-ctx-sem-r2 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow, the dry prediction, the prechecks and this
file are frozen by sha256 in `ctx-compress-sem-r2-FREEZE.sha256` (checked in every abab job and in the verdict job).
Synthetic text only. Builds on C1-ctx-sem-r1 (PR #184, run 46088778796, sha 1a1d2273, REVERT: failed G1, G1s, G8). The
blind set is r15's, byte for byte. `brief_compress.py` (r14) and `brief_compress_sem.py` (sem-r1) are untouched
(test-checked). PRs #181 / #183 are not touched.

## Question
Can the semantic supersession that sem-r1 showed (7/9 blind updates CORRECT) be kept while removing its three failures:
the still-true pair merges (G1), the static fact displaced by budget (G1s), and the 11.6 s per-brief model cost (G8)?

## Change vs sem-r1: a mechanism BUNDLE of three parts (disclosed plainly)
This round changes the placement of the model work and adds a budget rule. It is NOT a single-factor change. The three
parts are scored together (arm B); two report-only ablations are computed from the same B blocks at no model cost
(presence of the current value in the brief only, not answered by the model):
1. **Cached per-statement keys.** Each value-bearing user statement (sem-r1's `statement()`: first sentence with a word
   holding a digit, at most 160 chars) is keyed once, by its own greedy request (KEY_PROMPT), when it arrives (off the
   voice path). The key is cached under sha256 of the NFC, whitespace-collapsed statement. At brief time keys are
   lookups.
2. **Collision judge.** Two keys collide when their collision forms (digit words dropped, a plural "s" dropped) are
   equal, or the shorter has >= 2 words that are all in the longer. A statement's partner is the latest earlier
   statement whose key collides (equal preferred over subset). Only then one short greedy request (JUDGE_PROMPT) says
   REPLACE or ALSO; the verdict is cached under sha256 of the pair. REPLACE drops the partner; ALSO / unparsable / error
   keeps both. A replacing statement is tried at the shortest size of itself and what it (transitively) replaced (r7).
   Ablation **B-nojudge**: same keys, every collision treated as REPLACE.
3. **Displacement rule.** A superseding line may not displace a static fact: every fact exchange r4 keeps that is
   neither dropped nor superseding is reserved before the greedy fill. Ablation **B-nodisp**: B's verdicts, no rule.
Fallback = r4 byte for byte: no cache, no candidate, > 24 candidates, no REPLACE verdict, or a brief within budget
(test-checked on all 21 briefs, plus failing / all-distinct / all-ALSO stand-ins and degenerate inputs).

### Where the model work runs in the harness (and what G8a measures)
In a B block, after one unscored warm-up request (every arm), for each scenario: ARRIVAL = the brief's statements are fed
one at a time in conversation order to a fresh per-block cache (`KeyCache.arrive`: one key request per new statement
and, if its key collides, one judge request against its partner), timed per statement (G8b); then BRIEF =
`compress_brief_sem2_info` with the warm cache, timed around the whole call (G8a: lookups + any key/judge miss + the
selection). **Disclosure on G8a:** the parent's text lists "cache lookup + collision judges, cache warm". Here the judge
verdicts are cached too, computed when the newer statement arrives (both statements are known then), so with a warm
cache a brief makes no model call and G8a is expected near 0. Because that makes G8a easy, the verdict also reports, as
`judge_in_path_per_brief`, the sum of the brief's judge walls: what G8a would cost if the judges ran per brief instead
of on arrival. G8a is still a real gate: any key or judge miss at brief time is a live model call inside the timed wall,
and any fallback or model-call error in a B block fails it.

## What failed in sem-r1, and what r2 changes for each item
| sem-r1 item | what failed (M, run 46088778796) | r2 change | expected (I) |
|---|---|---|---|
| G1 vet p_old | "favorite vet tech TECH-9" and "go-to vet tech TECH-12" got one key; key-only grouping dropped the older true fact | collision -> judge; ALSO keeps both | fixed (precheck: ALSO) |
| G1 car p_old | the garbled sentence naming FLT-06 and FLT-07 shared FLT-06's key; FLT-06's own line dropped, model OFF_FORMAT | judge sees the pair | NOT fixed (precheck: judge says REPLACE; the garbled line does read as an update) |
| G1s car s1 | longer superseding lines took the room of a static fact (oracle with perfect keys lost it too) | displacement rule reserves r4-kept statics | fixed by construction (precheck 11/11) at a cost in update recall (oracle: 6/9 CORRECT with the rule vs 9/9 without) |
| G8 11.6 s per brief | one batched ~400-token pass per brief on CPU | model work per statement on arrival, cached | G8a near 0; per-statement cost moves to G8b (report-only, off the voice path) |
| G1e vet u2 / u3 | "number" vs "ID", "door code" vs "access code" got different keys | single-statement keys with a "plainest common words" instruction; subset collision | u3 fixed in precheck ("door code" both); u2 still missed ("microchip number" vs "chip id") |
| prompt tuned on dev only | yes (3 iterations) | yes (2 iterations, below) | - |

## Design choices made with, and without, the blind set (disclosure)
- KEY_PROMPT and JUDGE_PROMPT were written once and never changed. The collision rule was developed on dev sets only
  (main, r14, r13, r12, recency, r8), local Metal run of the pinned gguf: v1 (equal keys only): dev stale 36 of 51
  fixed, 0 old facts dropped, but 15 left ("shed key" vs "shed key tag", "drink code" vs "interval drinks code" ...);
  v2 (final: + subset of >= 2 words, digit words and plural "s" dropped): dev stale 44 of 51 fixed (same count as
  sem-r1's final prompt), 0 dev old facts dropped. The r15 set was sent to the model only after v2 was frozen.
- v1 judged every older member of a key group per brief; on main the "workshop note N" fillers share a key, which meant
  11-13 judges per brief (about 30 s on CPU). The partner rule (one judge per arriving statement) came from that dev
  observation.
- The displacement rule is the parent's specification. Its cost was measured on the blind set by the deterministic
  oracle (perfect keys + perfect judge, `ctx_compress_sem_r2_oracle_bound.json`): updates CURRENT 6/9 with the rule vs
  9/9 without, superseded 0/9 either way, pair-old 3/3 and statics 11/11 either way. So on this set under perfect keys
  the rule protects nothing and costs 3 current values; with real model keys (precheck) B-nodisp loses car s1, the rule
  keeps it and costs 4 current values. The rule was kept as specified; this disclosure is the reason the parent's
  "keeps >= 7/9 updates fixed" is predicted to hold for STALE removed but not for CORRECT.

## Arms (all via `tiers.snapshot`, 6000 cap, budget 2810)
Model gemma-4-E2B-it Q4_K_M (sha 740185b2..., pinned), llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy seed 42, no prompt
cache, fresh llama-server per block, the r15 / sem-r1 workflow (24 jobs, same matrix, deadlines, job timeouts).
A = r4 byte copy; B = sem-r2; R = sem-r1 (live batched pass, report-only, R1 on r15); O = full brief (report-only).
Blocks: B A B A O (main); B A B A O R (r15); B A B A (dev).

## Primary metric, n, gates (`bench/ctx_compress_sem_r2_verdict.py`)
Primary metric: r15 update STALE and CORRECT (9 per block) with r15 pair-old LOSS (3 per block). Counts per primary block
(A1 B1 A2 B2). band = max(A1 vs A2 flips, B1 vs B2 flips) pooled over the 103 scored r15 / dev probes per block (A/A rule;
sem-r1 band was 0). n: r15 9 updates, 3 pair-old, 2 pair-new (report), 11 static.
- **NULL:** as sem-r1 (r2 verdict NULL; scenarios / incomplete job / missing primary block / request ERROR / category
  counts; no presence entry), plus a B row without `sem` / `in_brief` / `in_brief_ablation`, a job without sem_harness
  meta `ctx-compress-sem-r2`, no B sample.
- **G1e (PRIMARY):** max(STALE_B) <= min(STALE_A) - 3 - band over the 9 updates, and min(CORRECT_B) >= 3.
- **G1 (PRIMARY):** max(LOSS_B) <= max(LOSS_A) + band over the 3 pair-old facts.
- **G1s:** min(RECALL_B) >= max(RECALL_A) - band over the 11 static facts.
- **G1b / G1c / G2 / G3 / G4 / G5 / G6 / G7:** unchanged from sem-r1.
- **G8a brief_latency:** p50 brief-time added wall (warm cache) per distinct (job, B block, brief) <= 3.0 s, no fallback
  and no model-call error in any B block. About 54 samples.
- **G8b (report-only):** per-new-statement arrival cost (key + judge if colliding) p50 / p95 / max; cold total per brief
  (all statements keyed and judged from an empty cache); judge-in-path per brief.
- KEEP iff every gate passes; otherwise REVERT, listing every failed gate. Report-only: r15 current value in brief for
  B, B-nojudge, B-nodisp and R1 (sem-r1); R1 / O1 answers; STALE / CORRECT splits; key disagreement B1 vs B2.

## Deterministic precheck and dry prediction (written before the scored run)
`ctx_compress_sem_r2_precheck_model.json` (one local Metal run, same gguf and llama.cpp b12527: (I) CI numerics may
differ), `ctx_compress_sem_r2_oracle_bound.json`, `ctx_compress_sem_r2_dry.json`.
- STOP rule (checked): any dev old fact (r8 c_old, r12 a-d, r13 a2, r14 a3) that r4 holds and B drops => REVERT(design,
  precheck), no GHA run. Result: `stop = []`.
- Dev + main: superseded value left in brief 7 of 51 (A 51): main flight / room x3 (main unchanged: only filler
  collisions, judged ALSO, so B = r4 there) and r8-cycling b1 (not a gated probe); every gated dev stale probe clean.
- r15: updates superseded value left 1/9 (vet u2), current value in brief 4/9; pair-old 3/3 in brief (car FLT-06 only
  inside the garbled line); statics 11/11. Ablations: B-nojudge pair-old 2/3 (vet TECH-9 lost), B-nodisp statics 10/11.
- **Dry prediction (I): REVERT, failed G1 only** (car garbled sentence: LOSS 1 vs 0); G1e pass (STALE 1 vs 9, CORRECT
  about 4), G1s pass, G8a pass (< 0.05 s), G8b per new statement p50 1.5-3.5 s, cold 40-70 s per brief, judge-in-path
  12-20 s. Confidence moderate; the alternative is KEEP.
- Mock verdicts (`mock-smoke.py` in the evidence dir: fake server, heuristic stand-in keyer / judge) run the real CLI path
  end to end for all 27 job files; both REVERT (the heuristic stand-ins false-merge on dev; pipeline check only).

## Limits
- n = 3 blind scenarios; smoke-scale, not powered. One model, CPU only, greedy. With band 0 one flip decides a gate.
- Bundle: a KEEP or REVERT is about the three parts together; the ablations are presence-only.
- The arrival simulation feeds a whole conversation's statements just before the brief; in production the newest user
  statement may arrive in the same turn as the brief, then its key (and judge) would be on the voice path (G8b).
- Production consumer: `compress_brief_sem2` is not called by tiers.snapshot.

## Stop rule
- Exactly one scored run: the run started by the push that adds `.github/workflows/ctx-compress-sem-r2.yml` to
  `ci/ctx-sem-r2`; `gh workflow run` only if that push starts no run.
- A NULL from infra (GGUF download, runner loss, job timeout) may be re-run once at the identical commit
  (`gh run rerun --failed`). Infra failing twice => NULL(infra).
- Any other change (set, prompt, rule, gate, harness, budget, model) is a new experiment (sem-r3).
- No edits to frozen files after the scored run starts.

# C1-ctx-compress-r7 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict script are frozen by sha256 in `ctx-compress-r7-FREEZE.sha256`
(checked in-workflow). Synthetic text only. Builds on C1-ctx-compress-r6 (PR #171, run 45930678727, REVERT on the
unique-recall gate; `outcome_without_unique_gate` KEEP).

## What r6 showed, and what r7 changes for each item
- r4 (production since r5) tries facts strictly shortest first. When a newer superseding fact is a few percent longer
  than the old one, r4 keeps only the old value at the 2810 budget margin: STALE 6/6 per A block (measured in r6).
- r6 (10% size bands, newest first inside a band) fixed the stale answers (STALE 0) but dropped an old unique fact of
  the same size class (glaze 0/3), because newest-first ranks *every* older band member below newer ones, related or
  not. r7 changes this: only an older fact that fills the same slot as a newer fact loses its place. Unrelated old facts
  keep r4's order.
- r6's harness, recency set, scorer and verdict rule had no defect in the r6 run (dry prediction matched every item).
  r7 reuses them unchanged; the only scored-code change is the compressor.

## The ONE change (production)
`voice-adapter/brief_compress.py`, `_fact_order` (new) + `_slot` (new): a fact exchange's slot is the normalised
subject of the asker line's first own-attribute statement, regex `\bmy\s+(<1-5 words>)\s+(is|are|was|were)\b`
(case-insensitive, lazy, whitespace collapsed, lowercased). Facts with no slot, and facts whose slot no other fact
shares, keep r4's key (size ascending, newest first on equal sizes). Of two or more facts sharing a slot, only the
newest is tried, at key (min size among the slot's members, -index); the older members are dropped from the fact order.
Fact/filler classification, filler order, budget and fallbacks unchanged. The rule is generic (any "my X is" subject).
It is not keyed to the probe words (bench/badge/Update).

Recorded differential (unit test `test_brief_compress_r7`): r7 is byte-identical to r4 on the FULL, OTHER, slm-v2-e5
and slm-v2-live briefs at every r2 budget, and on the three main-set (r2 suite) briefs at 2810. It differs from r4 on the
three recency briefs (the supersede pairs). The r6 production code moves to `bench/brief_compress_r6.py` (byte copy,
sha256 c0c86407...). `test_brief_compress_r6` imports that copy. `test_ctx_compress_r6` inlines the 0.10 band constant
in its recency-suite size assertion. Neither change alters what those tests check. The r6 harness, verdict, PREREG and
freeze files are unchanged.

## Arms
- A = `bench/brief_compress_r4.py`: byte copy of the r4/r5 production compressor (sha256 5a88bc8b...), baseline.
- B = `brief_compress.py` (r7).
- O = full brief, no compression (report-only reference).
All via `tiers.snapshot` (6000 cap), budget 2810, same system prompt and call turns.

## Harness (frozen r6 harness, unchanged)
`bench/ctx_compress_r6_eval.py` (sha256 41d94624..., unchanged; its B arm imports production `brief_compress`, so B
is r7 with no harness edit; meta tag `r6_harness` unchanged), `--set main` (r2 suite, 18 planted, `context_eval.score`)
and `--set recency` (recency-1..3: supersede pairs bench/badge, old unique glaze; `score_recency`). 9 jobs (scenario x
lo/hi/rc) on ubuntu-24.04-arm, gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 CPU, block order B,A,B,A,O (labels
B1 A1 B2 A2 O1). Greedy, seed 42, max 120 tokens, thinking off, cache_prompt false, deadline 2580 s.

## Primary metric, n, A/A
As r6: primary = STALE on the 6 supersede probes per block. Guards: planted recall (18 main per block), glaze recall
(3 per block), INVENTED, first_sentence p50. Bands: main = A1 vs A2 per-item RECALL flips; rec band = A1 vs A2 per-item
flips over the 9 recency probes; t band = |p50(A1) - p50(A2)| (main jobs).

## Frozen rule
`bench/ctx_compress_r6_verdict.py` UNCHANGED (sha256 fcf28c47...), first match: NULL (infra/shape); REVERT (r2 + r5 main
gates; unique recall min(B) < max(A) - rec band; recency INVENTED B > max A; p50(B) > p50(A) + t band; stale min(B) >
max(A) + rec band); KEEP if stale max(B1,B2) < min(A1,A2) - rec band; else NO_UPDATE. Report-only:
`outcome_without_unique_gate`, O1, BOTH, B/B flips. The verdict JSON keys say "r6" because the script is the frozen r6
script. Its arm B is r7 here.

## Stop rule
One scored run. The push that adds `.github/workflows/ctx-compress-r7.yml` to `ci/ctx-compress-r7` triggers it (file-name
dispatch 404s while the file is not on the default branch, as in r6). A NULL from infra (timeout, request errors, runner
loss) may be re-run once with the identical commit (`gh run rerun` or `workflow_dispatch` at the same sha). No edits to
frozen files after the scored run starts.

## Prediction (I, stated before scoring)
Dry run (`ctx_compress_r6_eval.py --dry`, and `test_brief_compress_r7.test_recency_suite_dry_prediction`):
- Main set: B brief byte-identical to A in all 3 scenarios (2485 / 2485 / 2484 chars). Expect planted recall 18/18 both
  arms, A/A 0, p50 B - A inside the t band (identical prompts).
- Recency set: A (r4) holds OLD bench/badge + glaze, not NEW. B (r7) holds NEW bench/badge + glaze, not OLD (B brief
  2515 chars in recency-3). O holds everything.
- Mock smoke (fake server answering the last matching code in context; not scored): STALE A 6 / B 0, glaze A 3 / B 3,
  outcome KEEP, `outcome_without_unique_gate` KEEP.
- So the expected outcome is KEEP: B CORRECT 6/6, STALE 0, glaze 3/3 per block. Two things can change it. Gemma may
  answer differently from the context it sees. Or the latency gate may fail on recency-irrelevant noise: main-set
  prompts are byte-identical, so any p50 gap is runner noise against a t band measured on the same run.

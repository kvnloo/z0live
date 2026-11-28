# C1-ctx-compress-r6 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict scripts are frozen by sha256 in `ctx-compress-r6-FREEZE.sha256`
(checked in-workflow). Synthetic text only. Builds on C1-ctx-compress-r5 (PR #170, run 45924225366, KEEP).

This is the FULL r6 (code change + recency probe set), not the fallback.

## Question
r4 (kept in r5) tries fact exchanges strictly shortest first. Open risk: of two near-equal-length facts where the newer
supersedes the older, the older can be 1-2% shorter, be tried first, take the budget, and leave the newer value out
(recency loss). Does a tiered key (size bands, newest first inside a band) remove stale answers without losing planted
recall, adding invented answers or slowing the first sentence?

## The ONE change (production)
`voice-adapter/brief_compress.py`, `_fact_order`: facts are cut into size bands. A band starts at the shortest fact not
yet placed and holds every fact with size <= 1.10 x that size (`FACT_BAND = 0.10`, size = exchange chars incl. joins).
Bands are tried shortest band first; inside a band, newest first. r4: strict size ascending, newest first only on equal
sizes. Fact/filler classification, filler order, budget and fallbacks unchanged. Recorded differential: r6 is
byte-identical to r4 on the FULL, OTHER, slm-v2-e5 and slm-v2-live briefs at every r2 budget; it differs on the
context_eval briefs.

## Arms
- A = `bench/brief_compress_r4.py`: byte copy of the r4/r5 production compressor (sha256 5a88bc8b...), baseline.
- B = `brief_compress.py` (r6).
- O = full brief, no compression (report-only reference).
All via `tiers.snapshot` (6000 cap), budget 2810, same system prompt and call turns.

## Harness (frozen r2 harness + one addition, frozen before scoring)
`bench/ctx_compress_r6_eval.py` runs the frozen `ctx_compress_r2_eval.main()` (transport, fresh llama-server per
block, greedy, seed 42, max 120 tokens, thinking off, cache_prompt false, deadline 2580 s, JSON layout) with the arms
above and `--set`:
- `main`: the r2 suite (workshop-1..3, 8 probes each, 18 planted), `context_eval.score`, unchanged. 6 jobs (scenario x
  probe half lo/hi).
- `recency` (NEW): `recency_suite()`: each r2 scenario with chat noise `noise(18+2i, 6)` cut to `noise(10+2i, 6)` (full
  brief 5124/5499/5874 chars, under the 6000 cap) and five planted fact exchanges (user line + short assistant ack,
  reply <= 200 chars so each is a fact):
  - supersede pair `bench`: OLD `Fern-2x` early in the chat (364 chars), NEW `Moss-4x` as the second-newest exchange
    (393 chars, "Update: ... is now ..., not the old one.");
  - supersede pair `badge`: OLD `Teal-1x` early (365), NEW `Plum-6x` newest (394);
  - unique `glaze` `Opal-8x` early (365), never superseded (control for r6's mirror risk).
  Noise exchanges are 372-376 chars, so OLD < every noise < NEW, and NEW <= 1.10 x OLD (one band).
  3 probes per scenario ("What is my workshop bench number now?", "What is my badge colour code now?", "What is my glaze
  batch code?", each + " Please give just the code."). 3 jobs (half rc).
- `score_recency`: CORRECT = newest value present, older absent; STALE = older present, newest absent; BOTH = both
  (counted neither correct nor stale, report-only); ABSTAIN = SKIP or the frozen r5 abstention regex; INVENTED = any
  other `Word-12` code; OFF_FORMAT; ERROR = empty reply. Unique probe: RECALL / ABSTAIN / INVENTED / OFF_FORMAT / ERROR.
  Value match is case-insensitive, whole token (`Moss-480` is not `Moss-48`).
- 9 jobs on ubuntu-24.04-arm, gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 CPU, block order B,A,B,A,O (labels
  B1 A1 B2 A2 O1).

## Primary metric and n
Primary: STALE count on the 6 supersede probes per block (A1 B1 A2 B2). Guards: planted recall on the 18 main probes
per block; glaze recall (3 per block); INVENTED; first_sentence p50. n per block: 18 main planted + 6 unplanted + 6
supersede + 3 unique.

## A/A gates
- Main: band = A1 vs A2 per-item RECALL flips (r2 verdict, unchanged). B/B flips report-only.
- Recency: rec band = A1 vs A2 per-item result flips over the 9 recency probes. B/B report-only.
- Latency: t band = |p50(A1) - p50(A2)| of first_sentence_s over the main jobs.

## Frozen rule (`bench/ctx_compress_r6_verdict.py`, first match)
- NULL: r2 verdict NULL (unchanged reasons); or a recency job missing/incomplete, a missing primary block, a request
  ERROR in a primary recency block, recency scenarios != recency-1..3, or != 6 supersede / 3 unique per primary block;
  or latency missing.
- REVERT if any of:
  - r2 verdict + r5 gates (unchanged, main set only) give REVERT (planted recall min(B) < max(A) - band, or planted
    SKIP/SUPERSEDED/INVENTED, or unplanted WRONG/INVENTED, B above max A);
  - unique recall: min(B1, B2) < max(A1, A2) - rec band (a planted recency-set fact lost: same rule as planted recall);
  - recency INVENTED (supersede + unique) in a B block > max(A1, A2);
  - p50(B) > p50(A) + t band (pooled per arm, main jobs);
  - stale: min(B1, B2) > max(A1, A2) + rec band.
- KEEP: none of the above and max(B1, B2) STALE < min(A1, A2) STALE - rec band.
- NO_UPDATE otherwise (stale within the band, including both arms at 0).
- Report-only: `outcome_without_unique_gate` (the same rule minus the unique-recall gate, i.e. the keep rule as the
  brief first stated it), O1 numbers, BOTH counts, B/B flips.

The unique-recall gate is an addition to the brief's keep rule. Reason: the brief's rule says KEEP *only if* B planted
recall >= A; glaze is a planted fact in the new probe set, and newest-first inside a band can drop an old unique fact
the same way shortest-first drops a newer value. Both outcomes are written to the verdict so the conductor can read
either.

## Stop rule
One scored run. A NULL (infra, timeout, request errors) may be re-run once with the identical frozen commit; no edits
to frozen files after the scored run starts. No other re-runs.

## Expectation (I, stated before scoring; dry run in `test_brief_compress_r6.test_recency_suite_dry_prediction`)
- Main set: A and B both hold all 6 planted values in all 3 scenarios (dry); expect recall 18/18 both arms (r5's B =
  this A), A/A 0. B brief is ~10 chars longer (2495 vs 2485), so p50 within ~1 s.
- Recency set dry: A (r4) holds OLD bench/badge and glaze, not NEW, in all 3 scenarios: expect A STALE ~6 per block,
  glaze 3/3. B (r6) holds NEW bench/badge, not OLD, and not glaze: expect B CORRECT ~6, STALE 0, glaze 0/3 (abstain or
  invented). O holds everything.
- So the likely outcome is REVERT on the unique-recall gate, with `outcome_without_unique_gate` = KEEP: r6 trades the
  recency loss for an old-unique-fact loss at the same budget margin. A different outcome needs the model to answer
  differently from the context it sees (e.g. A abstaining instead of answering the stale value).

## Mock smoke (not scored)
`ctx_compress_r6_eval` + verdict run end to end with a fake server that answers the last matching code in the context:
STALE A 6 / B 0, glaze A 3 / B 0, outcome REVERT (unique recall), without unique gate KEEP.

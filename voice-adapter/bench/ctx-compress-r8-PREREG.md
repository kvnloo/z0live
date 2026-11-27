# C1-ctx-compress-r8 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict script are frozen by sha256 in `ctx-compress-r8-FREEZE.sha256`
(checked in-workflow). Synthetic text only. Builds on C1-ctx-compress-r7 (PR #172, run 45936350390, KEEP on the r6
recency set: B STALE 0/6, glaze 3/3, main 18/18).

## Open question
The r7 slot rule is lexical (`\bmy\s+(<1-5 words>)\s+(is|are|was|were)\b`, normalised subject). r7 was scored only on
the set it was designed against (bench/badge/glaze, one wording). Does it transfer to new subjects and wordings, and
does it ever drop a true older fact because a different fact shares its lexical slot (false merge)?

## What failed in prior preregs, and what r8 changes for each item
- r6 (newest-first inside a 10% band): the rule dropped an unrelated old fact (glaze 0/3), because the recency set was
  built around the change and the unique-fact control was report-only in the brief's first keep rule. r7 added the
  slot rule and made unique recall a gate. r8 keeps the unique control (d) as a KEEP gate.
- r7 scored the slot rule on the same three scenarios and the same "Update: my X is now" wording it was written
  against. It had no held-out set, no rephrased update, and no case where two different true facts share a slot.
  r8 adds all three, on new topics (allotment, cycling club, choir), new subjects and new update wordings. The
  scenarios and predictions are written and frozen before any scored run.
- r6/r7 band = A/A flips only. r8 band = max(A1 vs A2 flips, B1 vs B2 flips) over all r8 probes.
- r7 gated latency. Main-set briefs are byte-identical between arms (dry run), so r8 keeps latency report-only.

## The change under test
None in production. `voice-adapter/brief_compress.py` is byte-identical to r7 (sha256 e22e84b6..., asserted by
`test_ctx_compress_r8.test_production_is_r7` and the freeze). r8 adds harness files only:
`bench/ctx_compress_r8_eval.py`, `bench/ctx_compress_r8_verdict.py`, `test_ctx_compress_r8.py`, this PREREG, the
freeze file and `.github/workflows/ctx-compress-r8.yml`.

## Arms
- A = `bench/brief_compress_r4.py` (r4 byte copy, sha256 5a88bc8b...), baseline.
- B = `brief_compress.py` (r7 supersession-aware fact key).
- O = full brief, no compression (report-only).
All via `tiers.snapshot` (6000 cap), budget 2810, same system prompt and call turns (the arms are the frozen r6
harness's `COMPRESS`).

## r8 suite (held out)
`r8_suite()`: 3 scenarios (r8-allotment, r8-cycling, r8-choir). Stable brief and call turns are the r2 scenarios' own,
so prompt shape matches r2/r6. Chat = 6 old facts, 4 topic noise exchanges, 5 new facts. Each OLD exchange is
shorter than every noise exchange and each NEW one is longer (NEW at most 12% longer than the shortest OLD; sizes
padded deterministically). So r4's shortest-first order reaches the old facts first and the new ones last at the
2810 margin, as in r6. 7 probes per scenario (order a1 a2 d b1 b2 c_old c_new), scored by
`ctx_compress_r6_eval.score_recency` (unchanged).
- (a) SAME, keys a1 a2: the old value is "My <subject> is X." and the update is "<prefix> my <subject> is now Y, ...".
  Subjects: allotment plot number, shed key tag, club jersey number, bike rack spot, choir folder number, rehearsal
  seat code (none from r6). Prefixes: "Heads up,", "Small change:", "Quick update,", "One change:", "Note that",
  "Small update:".
- (b) REPH, keys b1 b2: the old value is in the slot pattern. The update does not match it: "I moved the X ... reads
  Y", "it changed to Y", "We switched to ... Y". Report-only coverage.
- (c) FM, key c_old (gated) and c_new (report-only): "My <thing> is the <colour> one tagged X." and later "My <thing> is
  at/booked in ..., the ticket/job card is Y." (wheelbarrow, bike, robe). Both facts are true. The probes ask for the
  older tag (c_old) and the newer ticket (c_new).
- (d) UNIQ, key d: one old unique fact (seed order reference, helmet sticker code, music stand label).

## Offline dry prediction (I, frozen before scoring; dry-r8.json, test_ctx_compress_r8.test_dry_prediction)
Context held by each arm, identical across the 3 scenarios:
| probe | A (r4) | B (r7) | O |
|---|---|---|---|
| a1, a2 | OLD only (STALE) | NEW only | both |
| b1, b2 | OLD only (STALE) | OLD only (STALE) | both |
| c_old | present | ABSENT (false merge) | present |
| c_new | ABSENT | present | present |
| d | present | present | present |
Brief chars at 2810: A 2528/2532/2539, B 2623/2614/2621, O 5506/5494/5506. Main set: B byte-identical to A
(2485/2485/2484), as in r7.
Mock smoke (fake server that answers the newest of the probe's values present in its context; not scored):
same_stale A 6 / B 0, false_merge_loss A 0 / B 3, unique 3/3 both, reph stale 6 both, outcome REVERT-r7.
If Gemma answers from what its context holds, the expected outcome is **REVERT-r7**. The (a) transfer part should
also pass (B STALE 0 vs A 6). The false merge follows from the rule's construction; the scored run measures whether
it reaches the answers.

## Primary metrics, n, band
Per primary block (A1 B1 A2 B2), summed over 3 scenarios: SAME (6 probes) CORRECT/STALE/BOTH/ABSTAIN/INVENTED; REPH (6)
same classes; FM c_old (3) RECALL and LOSS = 3 - RECALL; FMNEW c_new (3) RECALL; UNIQ d (3) RECALL. Main set: 18
planted per block (r2 verdict + r5 gates, unchanged). band = max(A1 vs A2, B1 vs B2) per-item result flips over the
21 r8 probes. Latency: first_sentence p50 per arm over the main jobs (frozen r6 `latency`), report-only. n is small
(3 scenarios, one synthetic family per category). This is a smoke-scale transfer check, not a powered estimate.

## Frozen rule (`bench/ctx_compress_r8_verdict.py`, first match)
- NULL: r2 verdict NULL; an r8 job missing or incomplete; a missing A1 B1 A2 B2 block; a request ERROR in a primary
  r8 block; r8 scenarios != the 3 names; per-block counts != 6/6/3/3/3. A NULL from infra may be re-run once at the
  identical commit.
- REVERT-r7 (recommend not merging PR #172 as-is): min(LOSS_B1, LOSS_B2) > max(LOSS_A1, LOSS_A2) + band.
- KEEP-r7-generalises: all of (a) max(STALE_SAME_B) < min(STALE_SAME_A) - band; (c) max(LOSS_B) <= min(LOSS_A) + band;
  (d) min(UNIQ_B) >= max(UNIQ_A) - band; main: r2 outcome KEEP and r5 gates outcome != REVERT.
- NO_UPDATE otherwise.
Report-only: REPH (coverage), FMNEW, O1, latency, and each condition as a boolean in `checks`.

## Harness and transport (unchanged from r7)
12 jobs (scenario x lo/hi/ra/rb). lo/hi = r2 main set through the frozen r6 `install("main")`; ra = r8 probes 0,1,2;
rb = r8 probes 3,4,5,6. ubuntu-24.04-arm, gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 CPU. Block order
B,A,B,A,O (labels B1 A1 B2 A2 O1), fresh llama-server per block. Greedy, seed 42, max 120 tokens, thinking off,
cache_prompt false, deadline 2580 s, job timeout 55 min.

## Stop rule
One scored run, triggered by the push that adds `.github/workflows/ctx-compress-r8.yml` to `ci/ctx-compress-r8`. No
edits to frozen files after the scored run starts.

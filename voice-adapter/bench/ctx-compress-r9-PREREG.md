# C1-ctx-compress-r9 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict script are frozen by sha256 in `ctx-compress-r9-FREEZE.sha256`
(checked in-workflow). Synthetic text only. Builds on C1-ctx-compress-r8 (PR #173, run 45942470551, REVERT-r7: the r7
slot rule false-merged two different true facts that share one lexical slot, LOSS 3/3 in both B blocks).

## r8 lessons: what r8's prereg missed, and what r9 changes
Source: `ctx-compress-r8-PREREG.md` and the r8 RESULT (r8 evidence folder, outcome REVERT-r7).
- r8's prereg tested r7 for transfer and false merges, but had no candidate fix. r9 tests one: cue-gated supersession,
  which is one of the two r9 directions r8's RESULT named.
- r8 had no direct r7-vs-fix comparison. r9 runs r7 as a report-only rival arm R (block R1) on the recency and r8
  sets, so r9 and r7 are seen on the same runner and the same items.
- r8's verdict scored only its own held-out set. The fix to the r6 stale bug was gated in r7 on the r6 recency set,
  which r8 did not re-run. r9 re-runs every earlier set: the r2 main set, the r6 recency set (r7 reused it unchanged,
  so "r6" and "r7" recency are one set: bench/badge/glaze) and the r8 held-out set (a, b, c, d). The stale fix is a
  gate on both the r6 set and r8 (a).
- r8's band came only from r8 probes. r9 pools A1/A2 and B1/B2 flips over all 30 recency + r8 probes:
  band = max(AA flips, BB flips).
- r8 had a KEEP rule and a REVERT rule, and NO_UPDATE between them. r9 has one gate list: KEEP only if every gate
  passes, else REVERT, and the verdict names each failed gate.
- r8's (c) wording has no update cue by construction. So r9 passes (c) **by design** (I). This run cannot show
  whether the cue rule false-merges when a different-attribute statement contains a cue word (for example "my bike is
  now at the workshop"). That needs a new held-out set. It is out of scope here and listed as a limitation.

## The ONE change (production)
`voice-adapter/brief_compress.py`: new `CUE` regex and `_cue()`, rewritten `_fact_order`. `_slot` is unchanged from r7.
- `CUE` (frozen; whole words, case-insensitive, matched on the asker line after the speaker label, never on the reply):
  `now, change, changed, instead, new, no longer, not the old, update, updated, moved, switched, replaced`.
  A word boundary is any character that is not a word character or `-` (so `renew`, `nowhere`, `exchange`, `updates`
  and `re-new` do not match).
- `_fact_order`: every fact starts with the r4 key (size ascending, newest first on equal sizes). For each slot shared by
  2 or more facts, take the newest member (other than the oldest) whose asker text has a cue. That member supersedes
  every older member of the slot. The older members are dropped, and it is tried at the shortest size among itself and
  the members it replaces (r7's priority rule). Members newer than it keep their r4 keys. A slot with no cue member
  keeps r4 order, so with no cue the order and output are byte-identical to r4.
- r7's production code moves to `bench/brief_compress_r7.py` (byte copy, sha256 e22e84b6...). `test_brief_compress_r7`
  and `test_ctx_compress_r8` now import that copy (r8's arm-B dry prediction is pinned to it), so they still check
  what they checked before. All r2..r8 harness, verdict and prereg files are unchanged.

## Offline dry check of the earlier set wordings (I, done before writing the change)
Every newer same-slot fact in the r6 recency set and in r8 (a) contains a cue. Every newer same-slot fact in r8 (c)
contains none. So the cue rule keeps the stale fix and should remove the false merge:
- r6 recency, bench and badge updates: "Update: my ... is now ..., not the old one" (cues: update, now, not the old).
- r8 (a): allotment plot number / choir folder number: "now" only. shed key tag, club jersey number, bike rack spot,
  rehearsal seat code: "now" plus change/update/new/moved/changed. Without "now" in the list, the fix would be lost on
  2 of the 6 (a) pairs and stay only through the other words on 4.
- r8 (c) newer members ("My wheelbarrow is at the repair stand today, the ticket ...", "My bike is booked in at the
  workshop this week, the job card ...", "My robe is at the dry cleaner this week, the ticket ..."): no cue.
- r8 (b) updates contain cues (moved, changed, switched, new, replaced, now) but have no slot, so r9 = r4 = r7 there.
- r2 main set: no shared slot (one cue-word exchange, "Change of plan ... moved", has no slot). r9 = r4 byte-identical.
The cue list was frozen from the r8 RESULT's suggested words plus "update" and "change". It is not tuned on any
scored output, and it is not edited after the scored run starts.

## Arms
- A = `bench/brief_compress_r4.py` (r4 byte copy, sha256 5a88bc8b...), baseline.
- B = `brief_compress.py` (r9).
- R = `bench/brief_compress_r7.py` (r7 byte copy), report-only rival, block R1, recency and r8 jobs only (the main-set
  briefs are byte-identical across A, B and R in the dry run, so R1 on main would add runtime and no information).
- O = full brief, report-only (block O1).
All via `tiers.snapshot` (6000 cap), budget 2810, same system prompt and call turns.

## Harness
`bench/ctx_compress_r9_eval.py` over the frozen `ctx_compress_r2_eval.main()` (transport, blocks, deadline and JSON
unchanged). `--set main` = r2 suite (workshop-1..3, 18 planted, `context_eval.score`). `--set recency` = r6
`recency_suite()`, `--set r8` = r8 `r8_suite()`, both scored by `ctx_compress_r6_eval.score_recency` (unchanged).
15 jobs = scenario 0..2 x half lo/hi (main probes 0-3, 4-7), rc (recency 0-2), ra (r8 0-2), rb (r8 3-6).
ubuntu-24.04-arm, gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 CPU. Blocks B,A,B,A,O (main) and
B,A,B,A,O,R (rc/ra/rb), labels B1 A1 B2 A2 O1 (R1), fresh llama-server per block. Greedy, seed 42, max 120 tokens,
thinking off, cache_prompt false, deadline 2580 s, step timeout 46 min, job timeout 55 min.

## Offline dry prediction (I, frozen before scoring; dry-r9.json, `test_ctx_compress_r9.test_dry_prediction`)
What each arm's context holds, the same in all 3 scenarios of each set:
| probe | A (r4) | B (r9) | R (r7) | O |
|---|---|---|---|---|
| recency bench, badge | OLD only (STALE) | NEW only | NEW only | both |
| recency glaze (unique) | present | present | present | present |
| r8 a1, a2 | OLD only (STALE) | NEW only | NEW only | both |
| r8 b1, b2 (rephrased) | OLD only (STALE) | OLD only (STALE) | OLD only (STALE) | both |
| r8 c_old (false-merge older fact) | present | present | ABSENT | present |
| r8 c_new | ABSENT | ABSENT | present | present |
| r8 d (unique) | present | present | present | present |
Brief chars at 2810: recency A 2458/2458/2457, B = R 2516/2516/2515. r8 A 2528/2532/2539, B 2598/2588/2591, R
2623/2614/2621. Main set: A = B = R 2485/2485/2484.
Mock smoke (fake server that answers the newest probe value present in its context; not scored, mock-verdict.json):
outcome KEEP. fm_loss A 0 / B 0 / R 3. Recency stale A 6 / B 0. r8a stale A 6 / B 0. glaze 3 and d 3 in every block.
REPH stale 6 in A, B and R. c_new recall: A 0, B 0, R 3.
Expected outcome if Gemma answers from what its context holds: **KEEP**. r9 should match r7 on the recency set and on
r8 (a), and match r4 on r8 (c). Known cost (I): on r8 (c), c_new stays ABSENT as in r4 (both facts compete at r4 sizes,
and the newer one is longer). r7 had c_new present. This is report-only.

## Gates and frozen rule (`bench/ctx_compress_r9_verdict.py`)
Per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips) over the 30
recency + r8 probes (a flip = the result string differs).
- NULL: r2 verdict NULL, or any r6-recency / r8 NULL reason (missing or incomplete job, missing primary block, request
  ERROR in a primary block, wrong scenarios, per-block counts not 6/3 recency or 6/6/3/3/3 r8). A NULL from infra may
  be re-run once at the identical commit.
- G1 fm_loss (**PRIMARY**): max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band. LOSS = 3 - c_old RECALL.
- G2 recency_stale_fixed: max(STALE_B) < min(STALE_A) - band (r6 recency set, 6 supersede probes).
- G3 r8a_stale_fixed: max(STALE_B) < min(STALE_A) - band (r8 a1 a2, 6 probes).
- G4 unique_kept: min(RECALL_B) >= max(RECALL_A) - band, for glaze (3) and r8 d (3), each.
- G5 main_recall: r2 frozen verdict outcome KEEP (18 planted, min B >= max A - main band).
- G6 r5_gates: no r5 gate failure (planted SKIP/SUPERSEDED/INVENTED; unplanted WRONG/INVENTED; B <= max A).
- G7 no_new_invented: per B block, INVENTED over all 30 recency + r8 probes <= max(A1, A2) + band.
- KEEP iff every gate passes. Otherwise REVERT, and `failed` lists every failed gate.
Report-only: r8 (b) REPH counts, r8 c_new, O1, R1 per category, first_sentence p50 (frozen r6 `latency`, main jobs;
main briefs are byte-identical between arms, so a latency gate would only measure runner noise), A/A and B/B flips per
set.

## n and limits
n is small: 3 scenarios per set, one synthetic family per category, probes written by the experimenter. One model, CPU
only, greedy. The (c) case has no cue by construction, so G1 cannot catch a cue-word false merge (see r8 lessons).
This is a smoke-scale check, not a powered estimate.

## Stop rule
One scored run, triggered by the push that adds `.github/workflows/ctx-compress-r9.yml` to `ci/ctx-compress-r9`. No
edits to frozen files (code, cue list, harness, verdict, this PREREG) after the scored run starts.

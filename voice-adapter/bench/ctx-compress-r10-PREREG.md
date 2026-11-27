# C1-ctx-compress-r10 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script and this file are frozen by sha256 in
`ctx-compress-r10-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text only. Builds on
C1-ctx-compress-r9 (PR #174, run 45950209830, REVERT on G7 only). This is a new preregistration with a new held-out
set. It is not a re-score of r9.

## r9 lessons: what r9's prereg missed, and what r10 changes
- r9's G7 (no_new_invented) counted INVENTED over all 30 recency + r8 probes, including r8 c_new, whose planted value
  is absent from BOTH the A and the B brief (predicted in dry-r9.json). On such a probe neither arm can recall the
  fact, so an INVENTED answer measures how Gemma answers a missing fact on a slightly different prompt, not a fact the
  compressor lost. r9 failed only that gate (B 3 vs A 2, all from r8-cycling c_new). r10 splits the gate: INVENTED is
  gated only over probes whose planted value (`probe.current`) is a substring of the A brief or the B brief
  (`ctx_compress_r10_eval.presence()`, deterministic, computed in code from the frozen suites and arms). Probes absent
  from both briefs are report-only (`invented_absent_report_only`). Declared here before scoring.
  With the frozen sets, 9 of the 51 recency + r8 + r10 probes are absent from both briefs: r8 b1, b2, c_new in each
  of the 3 r8 scenarios (b1/b2: only the old value survives in A and B). The other 42 are gated.
- r9's (c) false-merge set had no update cue by construction, so r9 passed it by design. r9's RESULT named the open
  risk: a different-attribute statement in the same lexical slot that contains a cue word ("my bike is now at the
  workshop"). r10 adds a held-out set built for exactly that case and makes it the primary gate.
- r9's mock smoke assumed abstention on missing facts. r10 runs the mock in two modes (abstain, and invent a fixed
  unrelated code) to check the gate split does what it says in both.
- r9's dry prediction gave context content only. r10 keeps that, and states the expected outcome per gate.

## The ONE production rule (unchanged)
`voice-adapter/brief_compress.py` is byte-identical to r9 (sha256 cb1cb6b8..., in the freeze). r10 changes only the
harness, the held-out set, the verdict and this prereg. Arms (all via `tiers.snapshot`, 6000 cap, budget 2810):
- A = `bench/brief_compress_r4.py` (r4 byte copy, 5a88bc8b...), baseline.
- B = `brief_compress.py` (r9 rule: same-slot supersession only when the newer asker line carries a CUE word).
- R = `bench/brief_compress_r7.py` (r7 byte copy, e22e84b6...), report-only rival, block R1 on probe sets.
- O = full brief, report-only, block O1.

## New held-out set: r10 (`ctx_compress_r10_eval.r10_suite`)
3 new scenarios with subjects not used in r2/r6/r8: r10-rowing, r10-climbing, r10-library. Stable brief and call
turns are the r2 scenarios' own (as r8). Chat = 5 older facts, 4 noise exchanges, 4 newer facts. Code families are
new (Poplar Spruce Maple Aspen Rowan Hazel Larch Holly Linden), none shared with the stable briefs or r6/r8.
Older facts are padded to <= 340 chars, noise sits at 352-359, newer facts at 375-381 (checked by test), so r4's
shortest-first order reaches the older facts first and the newer ones last at the 2810 margin. Per scenario:
- (e) CFM, PRIMARY. Two cue-word false-merge pairs. Older: "My <thing> is the <colour> one tagged X." Newer, same
  lexical slot, a DIFFERENT attribute, with an r9 cue word: kayak "is now at the repair bay ...", lifejacket "is in the
  new drying room ...", harness "is now hanging in the inspection cage ...", rope "was moved to the back store ...",
  umbrella "is now in the lost property box ...", tote bag "is at the new cloakroom desk ...". Both facts stay true.
  Probes: the older tag (e1_old, e2_old; gated, LOSS = 6 - RECALL per block) and the newer ticket (e1_new, e2_new;
  report-only recall, gated for INVENTED by presence).
- (a) SAME. Two true same-attribute updates with cues ("... instead", "... is updated to", "... since they replaced
  the cards", "... is no longer the old one, it is ...", "Update: ... changed over", "... after they switched the
  layout"). Probes "What is my <subject> now?"; CORRECT / STALE / BOTH / ...
- (d) UNIQ. One older unique fact, never superseded.
7 probes per scenario, order a1 a2 d e1_old e2_old e1_new e2_new, scored by `ctx_compress_r6_eval.score_recency`
(unchanged). Jobs rd (probes 0-3) and re (4-6).

## Harness and run
`bench/ctx_compress_r10_eval.py` over the frozen `ctx_compress_r2_eval.main()` (transport, blocks, deadline, JSON
unchanged). Sets: main (r2 suite, 18 planted, `context_eval.score`), recency (r6), r8 (r8 a/b/c/d), r10 (new), all
re-run. 21 jobs = scenario 0..2 x half lo/hi (main 0-3, 4-7), rc (recency 0-2), ra (r8 0-2), rb (r8 3-6), rd (r10
0-3), re (r10 4-6). ubuntu-24.04-arm, gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 CPU. Blocks B,A,B,A,O
(main) and B,A,B,A,O,R (probe sets), labels B1 A1 B2 A2 O1 (R1), fresh llama-server per block. Greedy, seed 42, max
120 tokens, thinking off, cache_prompt false, deadline 2580 s, step timeout 46 min, job timeout 55 min, verdict job
timeout 10 min.

## Offline dry prediction (I, frozen before scoring; dry-r10.json, presence-r10.json, `test_ctx_compress_r10`)
What each arm's context holds, the same in all 3 scenarios of each set. Recency, r8 and main rows are r9's.
| probe | A (r4) | B (r9) | R (r7) | O |
|---|---|---|---|---|
| r10 a1, a2 (same attribute, cue) | OLD only (STALE) | NEW only | NEW only | both |
| r10 d (unique) | present | present | present | present |
| r10 e1_old, e2_old (PRIMARY) | present | ABSENT | ABSENT | present |
| r10 e1_new, e2_new | ABSENT | present | present | present |
| recency bench, badge / r8 a1, a2 | OLD only | NEW only | NEW only | both |
| r8 c_old | present | present | ABSENT | present |
| r8 c_new, b1, b2 (current value) | ABSENT | ABSENT | c_new present; b1, b2 ABSENT | present |
r10 brief chars at 2810: A 2465/2469/2469, B = R 2634/2633/2638, O 4701/4699/4706 (rowing/climbing/library).
Mechanism (I): in each (e) pair the newer member shares the older one's slot and carries a cue, so r9 drops the older
member and promotes the newer one to the older one's size. The r9 rule cannot tell "is now at the workshop" (location)
from "is now Cedar-47" (same attribute).

Mock smoke (fake server that answers the newest probe value present in its context; not scored):
- mode abstain: REVERT, failed [G1_r10_cfm_loss]. r10 CFM LOSS A 0 / B 6 / R 6. r10 a STALE A 6 / B 0. Every other
  gate passes. INVENTED present 0 everywhere.
- mode invent (missing fact -> fixed code "Quartz-99"): REVERT, failed [G1_r10_cfm_loss]. INVENTED present A 6 / B 6
  (A on e_new, B on e_old), absent-in-both 3 / 3 (r8 c_new). G9 passes.
**Expected outcome if Gemma answers from what its context holds: REVERT, failing G1 (the primary gate).** If so, the
rival for r11 is an attribute-aware supersession key (the newer statement must restate the same attribute, not only
the same subject), not a longer cue list. A KEEP would mean Gemma recovered the older tags without them in context,
which the dry check says it cannot.

## Primary metric
r10 CFM LOSS (e1_old + e2_old, 6 probes per block, summed over 3 scenarios): LOSS = 6 - RECALL.

## Gates and frozen rule (`bench/ctx_compress_r10_verdict.py`)
Per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips) pooled over the
51 recency + r8 + r10 probes (a flip = the result string differs).
- NULL: r2 verdict NULL, or any recency / r8 / r10 NULL reason (missing or incomplete job, missing primary block,
  request ERROR in a primary block, wrong scenarios, per-block counts not 6/3 recency, 6/6/3/3/3 r8, 6/6/6/3 r10), or a
  scored probe with no presence entry. A NULL from infra may be re-run once at the identical commit.
- G1 r10_cfm_loss (**PRIMARY**): max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band.
- G2 r8_fm_loss: same rule on r8 c_old (3 per block) (r9's G1).
- G3 recency_stale_fixed: max(STALE_B) < min(STALE_A) - band (r6 set, 6 probes).
- G4 r8a_stale_fixed: max(STALE_B) < min(STALE_A) - band (r8 a1 a2, 6 probes).
- G5 r10a_stale_fixed: max(STALE_B) < min(STALE_A) - band (r10 a1 a2, 6 probes). Stale-fix transfer margin: strictly
  below A minus the band (with band 0 and A at 6: B <= 5).
- G6 unique_kept: min(RECALL_B) >= max(RECALL_A) - band, for r6 glaze, r8 d and r10 d (3 each), each.
- G7 main_recall: r2 frozen verdict outcome KEEP (18 planted, min B >= max A - main band).
- G8 r5_gates: no r5 gate failure (planted SKIP/SUPERSEDED/INVENTED; unplanted WRONG/INVENTED; B <= max A).
- G9 no_new_invented_present: per B block, INVENTED over the probes whose planted value is present in the A or the B
  brief <= max(A1, A2) + band.
- KEEP iff every gate passes. Otherwise REVERT, and `failed` lists every failed gate.
Report-only: INVENTED over the absent-in-both probes, r10 e_new recall, r8 (b) REPH and c_new, O1, R1 per category,
first_sentence p50 (frozen r6 `latency`, main jobs; main briefs are byte-identical between arms), flips per set.

## n and limits
n is small: 3 scenarios per set, 6 cue-word false-merge pairs in all, written by the experimenter. One model, CPU only,
greedy. Presence is a substring check on the brief, not a check that Gemma used the fact. Smoke-scale, not powered.

## Stop rule
One scored run, triggered by the push that adds `.github/workflows/ctx-compress-r10.yml` to `ci/ctx-compress-r10`. No
edits to frozen files (code, sets, harness, verdict, this PREREG) after the scored run starts.

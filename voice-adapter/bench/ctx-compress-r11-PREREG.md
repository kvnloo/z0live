# C1-ctx-compress-r11 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow and this file are frozen by sha256 in
`ctx-compress-r11-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text only. Builds on
C1-ctx-compress-r10 (PR #175, run 45957646838, REVERT on G1 only: the r9 rule lost the older fact of every cue-word
false-merge pair, 6/6 per B block). New production rule, same held-out sets and the same frozen r10 gates.

## The ONE production change
`voice-adapter/brief_compress.py`, `_fact_order`: supersession groups facts by **attribute** instead of by the slot
subject alone. Everything else is r9 (the r9 code is kept byte-identical as `bench/brief_compress_r9.py`, sha256
cb1cb6b8...).
- slot subject (unchanged, r7): `SLOT` = first `my <1-5 words> is/are/was/were` in the asker text after the speaker
  label, lowercased, whitespace collapsed.
- predicate head (new, `_attribute`): take the asker text after the slot's copula; delete every `CUE` match (the
  frozen r9 cue list: now, change(d), instead, new, no longer, not the old, update(d), moved, switched, replaced);
  split into words with `[\w'-]+`; walk the words in order, lowercased: a word containing a digit ends the walk with
  head `<value>`; a word in `FILLER` = {a, an, the, to, it, is, are, was, were, one, old} is skipped; the first other
  word is the head. No word left: head `<value>`.
- attribute = (slot subject, predicate head). Within one attribute group the r9 rule applies unchanged: the newest
  member whose asker text matches `CUE` drops every older member of the group and is tried at the shortest size among
  itself and them. No cue, or a cue statement whose head differs (a different attribute of the same subject):
  nothing is dropped and every member keeps r4's key, so the output is r4's byte for byte.
- Heads on the frozen sets (computed, `test_brief_compress_r11`): every (a) update and every recency / r8 superseder
  restates the value (`is now X`, `is X instead`, `is updated to X`, `is no longer the old one, it is X`): head
  `<value>` on both sides, so r11 = r9 there. r10 (e) pairs: kayak yellow vs at, lifejacket orange vs in, harness
  black vs hanging, rope green vs back, umbrella striped vs in, tote bag canvas vs at: different attributes, r11 = r4.
  r8 (c) pairs: wheelbarrow green vs at, bike red vs booked, robe long vs at (no cue, r4 already).

Arms (all via `tiers.snapshot`, 6000 cap, budget 2810, harness `bench/ctx_compress_r11_eval.py` over the frozen
`ctx_compress_r10_eval`, which runs the frozen r2 harness):
- A = `bench/brief_compress_r4.py` (r4 byte copy, 5a88bc8b...), baseline.
- B = `brief_compress.py` (r11).
- R = `bench/brief_compress_r9.py` (r9 byte copy), report-only rival, block R1 on probe sets.
- O = full brief, report-only, block O1.

## Deterministic pre-check (no model; `ctx_compress_r11_eval.py --precheck`, `test_ctx_compress_r11.test_precheck`)
r4, r9 and r11 on every main / recency / r8 / r10 brief at 2810; planted values present (1) or not (0), r4 r9 r11:
| set | probe | current | superseded |
|---|---|---|---|
| main (3) | studio, locker, vendor | 111 | - |
| main (3) | flight, room, parcel (current) | 000 | old values 111 |
| recency (3) | bench, badge | 011 | 100 |
| recency (3) | glaze | 111 | - |
| r8 (3) | a1, a2 | 011 | 100 |
| r8 (3) | d, c_old | 111 | - |
| r8 (3) | b1, b2 / c_new | 000 / 000 | b old 111 |
| r10 (3) | a1, a2 | 011 | 100 |
| r10 (3) | d | 111 | - |
| r10 (3) | e1_old, e2_old | 101 | - |
| r10 (3) | e1_new, e2_new | 010 | - |
Brief chars r4 / r9 / r11: main 2485/2485/2484 per scenario equal across arms (r11 = r4 byte for byte); recency
2458 / 2516 / 2516; r8 2528-2539 / 2588-2598 / = r9; r10 rowing 2465 / 2634 / 2547, climbing 2469 / 2633 / 2551,
library 2469 / 2638 / 2557.

**Finding (M, deterministic): r11 does NOT keep both facts of any cue-word false-merge pair within 2810. It is
budget-bound, not rule-bound.** r11 keeps both (e) pair members as candidates (nothing dropped), but the older facts
(<= 340 chars) and the two promoted (a) updates fill the brief to ~2550 chars, and each newer (e) fact is a 375-381
char exchange, so 2550 + 1 + 375 > 2810. No arm except O holds both: r4 holds old only, r9 new only, r11 old only.
The budget stays 2810. So the honest primary gate is the one r10 already froze: the OLDER fact must not be lost
(old-fact LOSS B <= A + band); the newer fact is report-only. r11 is predicted to hold exactly r4's (e) content and
r9's (a) content on the r10 set.

## Primary metric and gates (frozen r10 rule, unchanged; `bench/ctx_compress_r11_verdict.py` calls
`ctx_compress_r10_verdict.verdict` with the r11 presence map)
Per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips) pooled over the
51 recency + r8 + r10 probes.
- NULL: any r10 NULL reason (missing or incomplete job, missing primary block, request ERROR in a primary block, wrong
  scenarios, per-block counts, scored probe with no presence entry), or (new) any job without meta
  `r11_harness = ctx-compress-r11`, or no jobs. A NULL from infra may be re-run once at the identical commit.
- G1 r10_cfm_loss (**PRIMARY**): r10 cue-word false-merge OLD-fact loss (e1_old + e2_old, 6 per block):
  max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band. (A is predicted 0.)
- G2 r8_fm_loss: same rule on r8 c_old (3 per block).
- G3 recency_stale_fixed: max(STALE_B) < min(STALE_A) - band (r6 set, 6 probes).
- G4 r8a_stale_fixed: same on r8 a1 a2 (6).
- G5 r10a_stale_fixed: same on r10 a1 a2 (6).
- G6 unique_kept: min(RECALL_B) >= max(RECALL_A) - band for r6 glaze, r8 d, r10 d (3 each), each.
- G7 main_recall: r2 frozen verdict KEEP (18 planted).
- G8 r5_gates: no r5 gate failure.
- G9 no_new_invented_present: per B block, INVENTED over probes whose planted value is in the A (r4) or B (r11)
  brief <= max(A1, A2) + band. With r11 arms 15 of 51 probes are absent from both briefs (r8 b1, b2, c_new and r10
  e1_new, e2_new, x3 scenarios); 36 are gated.
- KEEP iff every gate passes. Otherwise REVERT with every failed gate listed.
- A/A and B/B flips are reported (`aa_flips`, `bb_flips`, per set) and enter every gate through the band; a band > 0
  widens every gate by that amount. Report-only: INVENTED on absent-in-both probes, r10 e_new recall, r8 REPH and
  c_new, O1, R1, latency.

## Offline dry prediction (I, frozen before scoring; `test_ctx_compress_r11`)
| probe | A (r4) | B (r11) | R (r9) | O |
|---|---|---|---|---|
| r10 a1, a2 | OLD only | NEW only | NEW only | both |
| r10 d | present | present | present | present |
| r10 e1_old, e2_old (PRIMARY) | present | present | ABSENT | present |
| r10 e1_new, e2_new | ABSENT | ABSENT | present | present |
| recency bench, badge / r8 a1, a2 | OLD only | NEW only | NEW only | both |
| r8 c_old, d | present | present | present | present |
| r8 c_new, b1, b2 (current) | ABSENT | ABSENT | ABSENT | present |
Mock verdicts (unit tests, fake answers from context): abstain on missing -> KEEP, r10 CFM LOSS A 0 / B 0 / R 6,
r10 a STALE A 6 / B 0; invent on missing -> KEEP, INVENTED present 0, absent-in-both B 9 (e_new 6, r8 c_new 3);
B losing the old facts (r9-like) -> REVERT [G1]. **Expected outcome if Gemma answers from its context: KEEP.** A
REVERT on G1 would mean Gemma lost old tags that are in its context.

## n and limits
3 scenarios per set, 6 cue-word false-merge pairs, written by the experimenter, already seen in r10 (the r10 set is
no longer held out for r11: the attribute key was designed after seeing it; recency / r8 / main are older sets). The
head rule is lexical: "is now the red one" vs "is the yellow one" is a different head (red vs yellow) though the same
attribute (colour), so a colour change would not supersede; "is at the gym" vs "is at the desk" share head "at" and
would. One model, CPU only, greedy. Presence is a substring check. Smoke-scale, not powered.

## Stop rule
One scored run, triggered by the push that adds `.github/workflows/ctx-compress-r11.yml` to `ci/ctx-compress-r11`. No
edits to frozen files after the scored run starts. Note: `test_ctx_compress_r10.py` is edited (its B arm pinned to
the r9 byte copy, since production moved to r11), so the r10 FREEZE no longer matches on this branch; the r11 FREEZE
covers the edited file.

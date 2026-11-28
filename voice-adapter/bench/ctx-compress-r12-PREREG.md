# C1-ctx-compress-r12 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow and this file are frozen by sha256 in
`ctx-compress-r12-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text only. Builds on
C1-ctx-compress-r11 (PR #176, run 46029688977, KEEP on the r10 sets). r11 was designed after the r10 set was seen,
so r12 is the first held-out test of the r11 rule.

## Question
Does the r11 attribute-aware supersession rule (newer fact supersedes only with an r9 update cue AND the same
attribute = (slot subject, predicate head)) transfer to wordings it has not seen, without losing older true facts?

## No production change
`voice-adapter/brief_compress.py` is r11, byte-identical to PR #176 (3dcf56c). Only a new set, a harness wrapper, a
verdict, tests and a workflow are added.

Arms (all via `tiers.snapshot`, 6000 cap, budget 2810, harness `bench/ctx_compress_r12_eval.py` over the frozen
`ctx_compress_r10_eval` / r2 harness; model gemma-4-E2B-it Q4_K_M, llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy
seed 42, fresh llama-server per block):
- A = `bench/brief_compress_r4.py` (r4, production baseline).
- B = `brief_compress.py` (r11).
- R = `bench/brief_compress_r7.py` (r7 slot-only supersession), report-only rival, block R1 on r12.
- O = full brief, report-only, block O1.
Blocks: B1 A1 B2 A2 O1 on main, B1 A1 B2 A2 O1 R1 on r12. 15 jobs = 3 scenarios x {lo, hi (main), rx, ry, rz (r12
probes 0-3, 4-7, 8-10)}.

## The r12 set (NEW, held out: campsite, coworking space, leisure centre)
Stable brief and call turns are the r2 scenarios' own (as r8 / r10). Per scenario: 7 older facts (<= 306 chars),
8 topic noise turns (4 exchanges, 353-358 chars each), 6 newer facts (377-382 chars). Code families Jade Onyx Topaz Amber
Coral Pearl Ruby Garnet Quartz Agate Beryl Jasper Zircon (unused by every earlier set, test-checked).
r11 head per statement (computed by `ctx_compress_r12_eval.attributes`, test-checked), and the r11 prediction:

| item | campsite | coworking | leisure | r11 prediction |
|---|---|---|---|---|
| (a) same head word, different attribute | van: "is booked for the ferry ... ref X" / "is now booked in at the new garage ... job card Y": booked / booked | locker: "is on the second floor ... tag X" / "is now on a new keypad system ... code Y": on / on | car: "is registered for the staff car park ... permit X" / "is now registered with the new charging bay app ... ID Y": registered / registered | heads COINCIDE + cue: r11 drops the older fact (false merge, a known limit of the lexical head) |
| (b) pronoun / plural subject | "Our tent is the green one tagged X" / "Our tent is now pitched on the new terrace ...": no slot (SLOT needs "my") | "My kids are both on the family pass ..." / "My kids are now in the new holiday club ...": both / in | "My parents are members on the senior plan ..." / "My parents are now booked into the new aqua class ...": members / booked | no slot or different head: r11 keeps the older fact (r7 drops it where there is a slot) |
| (c) multi-word predicate | cool box: "is kept in the shower block fridge" / "is due back at the new hire desk": kept / due | standing desk: "is set to the tall preset" / "is booked out for the new team": set / booked | gym bag: "is hanging on the peg rail" / "is waiting at the new lost property desk": hanging / waiting | different head: keep |
| (d) cue word only inside the value | bike trailer: "the orange one" / "parked by the **New** Barn gate": orange / parked | laptop: "the silver one" / "registered on the **Switched** On guest network": silver / registered | towel: "the red one" / "in the **Now** Dry laundry bin": red / in | cue fires (r9 / r7 would merge) but head differs: keep |
| (e) genuine update, cue, value restated (2) | pitch number "is X now", gate code "is changed to X" | desk number "is X after the move, they replaced ...", mailbox number "is now X" | lane booking code "is X now", class pass number "is changed to X" | head <value> both sides: supersede, stale fixed |
| (f) unique old fact | hook-up card number | printer PIN code | sauna token code | never superseded: keep |

Probes, 11 per scenario, order a_old b_old c_old d_old e1 e2 f a_new b_new c_new d_new; scored by
`ctx_compress_r6_eval.score_recency` (unchanged): old/new/unique probes RECALL / ABSTAIN / INVENTED / OFF_FORMAT /
ERROR; e1 e2 CORRECT / STALE / BOTH / ...

## Deterministic pre-check and offline dry prediction (M, no model; `--precheck`, `--dry`, test_ctx_compress_r12)
Brief chars at 2810, r4 / r7 / r9 / r11: campsite 2519 / 2567 / 2567 / 2757, coworking 2531 / 2660 / 2660 / 2775,
leisure 2524 / 2657 / 2657 / 2773; main 2485 / 2485 / 2484 for every arm (r11 = r4 byte for byte on main).
Planted value in each compressed brief (r4 r7 r9 r11; 1 = present):

| probe | campsite | coworking | leisure |
|---|---|---|---|
| a_old | 1000 | 1000 | 1000 |
| b_old | 1111 | 1001 | 1001 |
| c_old, d_old | 1001 | 1001 | 1001 |
| e1, e2 current / old | 0111 / 1000 | 0111 / 1000 | 0111 / 1000 |
| f | 1111 | 1111 | 1111 |
| a_new | 0111 | 0111 | 0111 |
| b_new | 0000 | 0110 | 0110 |
| c_new, d_new | 0110 | 0110 | 0110 |

Dry prediction per arm (context holds; same in all scenarios except R on campsite b): A: every old fact, e STALE, no
newer fact. B: a_old ABSENT, a_new present, b/c/d old present, e CURRENT, f present, b/c/d new ABSENT. R: every old
fact of a/c/d (and b where a slot exists) ABSENT, every newer fact present, e CURRENT. O: everything.
Presence map (A or B): 33 probes, 24 present in A or B, 9 absent in both (b_new, c_new, d_new x 3).
Mock verdicts (fake server answering from its context, real CLI path, `mock-smoke.py` in the evidence dir):
abstain -> REVERT [G1], FM LOSS A 0 / B 3 / R 11, (b)-(d) LOSS A 0 / B 0 / R 8, STALE A 6 / B 0, unique 3/3;
invent -> REVERT [G1], INVENTED present A 3 / B 3 (parity), absent-in-both 9 / 9.
**Expected outcome if Gemma answers from its context: REVERT on G1, caused only by the three (a) coinciding-head
items. (b)-(d) predicted loss-free for B, (e) stale fix predicted to hold.**

## Primary metric and gates (`bench/ctx_compress_r12_verdict.py`)
Per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips) over the 33 r12
probes.
- NULL: r2 verdict NULL; r12 scenarios != the 3 r12 names; incomplete job; missing primary block; request ERROR in a
  primary block; per-block category counts not as frozen (FM 12, SAME 6, UNIQ 3, FMNEW 12); a scored probe with no
  presence entry; a job without meta `r12_harness = ctx-compress-r12`; no jobs.
- G1 r12_fm_loss (**PRIMARY**): old-fact LOSS (not RECALL) over (a)-(d) a_old b_old c_old d_old, 12 per block:
  max(LOSS_B1, LOSS_B2) <= max(LOSS_A1, LOSS_A2) + band.
- G2 r12_stale_fixed: (e) e1 e2, 6 per block: max(STALE_B) < min(STALE_A) - band.
- G3 unique_kept: (f), 3 per block: min(RECALL_B) >= max(RECALL_A) - band.
- G4 main_recall: frozen r2 verdict KEEP (18 planted).
- G5 r5_gates: no r5 gate failure.
- G6 no_new_invented_present: max(INVENTED_B) <= max(INVENTED_A) + band over r12 probes present in the A or B brief.
- G7 latency: main first-sentence p50, B <= A + max(|p50 A1 - p50 A2|, 5% of A).
- KEEP iff every gate passes; otherwise REVERT with every failed gate listed.
- Report-only: (a) LOSS and (b)-(d) LOSS separately, newer-fact recall a_new..d_new, INVENTED over absent-in-both
  probes, O1, R1.

The recency / r8 / r10 sets are not re-run: B is byte-identical to the r11 scored run (46029688977), which measured
them (KEEP), and they are not held out.

## n and limits
3 scenarios, 12 false-merge old facts and 6 updates per block, written by the experimenter (who knew the r11 rule,
so the set probes its known lexical limit on purpose in (a)). One model, CPU only, greedy. Presence is a substring
check. The newer facts of (b)-(d) do not fit at 2810 for A or B (budget-bound, as r11 found); report-only.
Smoke-scale, not powered.

## Stop rule
One scored run: the run triggered by the push that adds `.github/workflows/ctx-compress-r12.yml` to
`ci/ctx-compress-r12` (dispatch by file name 404s while the file is not on the default branch; `gh workflow run` is
used only if that push starts no run). A NULL from infra (download, runner loss, job timeout) may be re-run once at
the identical commit (`gh run rerun --failed`, r11 precedent). Any other change (set, rule, gate, harness, budget,
model) = a new experiment (r13). No edits to frozen files after the scored run starts.

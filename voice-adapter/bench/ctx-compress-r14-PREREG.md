# C1-ctx-compress-r14 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow and this file are frozen by sha256 in
`ctx-compress-r14-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text only. Builds on
C1-ctx-compress-r13 (PR #178, run 46057100848): r13 (attribute key v2) was KEEP, with two predicted, report-only
limits that both showed up: (a3) same head and same second word but a different attribute -> B merged and lost the
old fact 3/3; (e3) a genuine update with the same head but a different second word -> B stayed STALE 3/3.

## What failed in earlier preregs, and what r14 changes
| round | outcome | what failed / was learned | how r14 handles it |
|---|---|---|---|
| r8 (r7 rule) | REVERT-r7 | slot-only supersession merged two different true facts in one lexical slot (old-fact LOSS 3/block) | supersession stays cue-gated and attribute-keyed |
| r9 | REVERT (G7) | INVENTED was gated over probes absent from both briefs; the mock assumed abstention, Gemma invents | INVENTED gated only over probes present in A or B (since r10), invent-mode mock run |
| r10 (r9 rule) | REVERT (G1) | a cue word on a different-attribute statement dropped the older true fact | attribute key (since r11) |
| r11 | KEEP | set seen before design; attempt 1 NULL from a GGUF download (infra) | fresh held-out set; one infra re-run allowed |
| r12 (r11 rule) | REVERT (G1) | same head word, different attribute: false merge (LOSS 3) | v2 (r13) kept; r12 re-run as dev, gated |
| r13 (v2) | KEEP | a3 merge and e3 stale were report-only limits; set written by the experimenter knowing the rule | **a3 and e3 are now the PRIMARY gated items on a new held-out set**; r13 becomes a dev set with no-regression gates |

## Question
Does a label tie-break (when the value is named by a label, the attribute is the slot subject + head word + that
label) keep the older true fact of an a3 pair AND supersede the stale value of an e3 update, without regressing any
earlier set?

## The ONE change (production)
`voice-adapter/brief_compress.py`: new `_value_label(predicate)` = the last `LABEL_WORDS = 2` non-FILLER words before
the first digit word of the predicate (CUE matches removed), taken only from the value's own clause (after the last
`, ; : .` before the value); `()` when there is no digit word or no such word. `_attribute` returns
(slot subject, head, `LABEL`, *label) when the label is non-empty, else r13's (slot subject, *v2 key). head = v2's
first word = r11's head (test-checked on every r12 / r13 / r14 statement), so r14 only ever splits r11 groups.
FILLER, WORD, CUE, VALUE, SLOT, KEY_WORDS are r13's, unchanged (test-checked). r13 is kept as
`bench/brief_compress_r13.py`, a byte copy of brief_compress.py at 4ec7dfd (sha256 d788b84a...).
`test_ctx_compress_r13` now pins its B arm, pre-check arm and attribute module to that copy.

Examples: "is booked for the harbour tour, the tour ref is X" -> (paddleboard, booked, LABEL, tour, ref) and "is now
booked for the new repair slot, the repair ticket is Y" -> (paddleboard, booked, LABEL, repair, ticket): two
attributes (v2 merged them). "is on the north jetty, the jetty tag is X" and "is now on the south jetty, the jetty tag
is Y" -> both (tender, on, LABEL, jetty, tag): one attribute (v2 split them). "is X", "is changed to X", "is X now, ..."
-> no label, v2 key (VALUE,) as before. "is number X" / "is now number Y" -> (number, LABEL, number) both.

**Chosen before any scoring and not switched.** RIVAL (recorded, report-only): **value shape** V = (slot subject,
head, digit/letter shape of the value: letter runs -> a, digit runs -> 9). Every planted code here has the shape
"a-9", so V is predicted to merge every same-head pair: it drops every a3 old fact (like r11) and fixes every e3.
V is evaluated **offline only** (pre-check and presence, `--presence-v`), not as a model block: one more block per
r14 job would push the r14 jobs (r13's took up to 38 min for 6 blocks) close to the 3000 s deadline. Arm R is r13.

## Arms (all via `tiers.snapshot`, 6000 cap, budget 2810)
Harness `bench/ctx_compress_r14_eval.py` over the frozen `ctx_compress_r10_eval` / r2 harness. Model gemma-4-E2B-it
Q4_K_M, llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy seed 42, fresh llama-server per block.
- A = `bench/brief_compress_r4.py` (r4, production baseline).
- B = `brief_compress.py` (r14, label tie-break).
- R = `bench/brief_compress_r13.py` (r13), report-only rival, block R1 on r14.
- O = full brief, report-only, block O1 on main and r14.

Blocks and jobs (24 = 3 scenarios x 8):
- main: B1 A1 B2 A2 O1, `lo` probes 0-3, `hi` 4-7.
- r14 (held-out): B1 A1 B2 A2 O1 R1, `hx` probes 0-4, `hy` 5-8.
- Dev and re-run sets, B1 A1 B2 A2:
  - `dw`: r13 probes 0-6 (a2x_old a2y_old e1 e2 f gated; a3_old e3 report-only).
  - `dx`: r12 probes 0-6 (a-d old, e1, e2, f).
  - `dy`: recency 0-2 (bench, badge, glaze), then r8 0,1,2,5 (a1, a2, d, c_old).
  - `dz`: r10 0-4 (a1, a2, d, e1_old, e2_old).

Deadlines: 2580 s (main, r10), 3000 s (r14, r13, r12), and 1400 s + 1500 s (recency, then r8). Step timeout 55 min,
job timeout 65 min.

## The r14 set (NEW, held out: airport, ski, theatre)
Stable brief and call turns are the r2 scenarios' own (as r8 / r10 / r12 / r13). Per scenario: 7 older facts
(293-300 chars), 8 topic noise turns (4 exchanges, 355-362 chars), 6 newer facts (376-382 chars). Code families
Cobalt Nickel Zinc Argon Neon Xenon Boron Radon Helium Carbon Iodine Sodium Osmium: no earlier set or stable brief
uses them (test-checked). Keys (`ctx_compress_r14_eval.keys`, test-checked):

| item | airport | ski | theatre | r13 (v2) | r14 prediction |
|---|---|---|---|---|---|
| (a3x) same head + same 2nd word, different attribute (**PRIMARY, old gated**) | bag: checked through ... bag tag / now checked through ... claim ref | skis: booked for ... race bib / wax ticket | umbrella: left at ... box slip / bar token | keys coincide + cue: drops old | labels differ: keep both |
| (a3y) same (**PRIMARY, old gated**) | ticket: linked with ... boarding pass / lounge pass (same noun, other modifier) | lift pass: loaded with ... ride ref / meal code | booking: paired with ... dinner ref / tour code | drops old | keep both |
| (e3x) genuine cued update, same head, different 2nd word, same label (**PRIMARY, stale gated**) | coat: at the west / east cloakroom, hook tag | boots: in the upper / lower hall, locker tag | scarf: in the stalls / circle cloakroom, peg tag | keys differ: stale | one key: supersede |
| (e3y) same (**PRIMARY, stale gated**) | pram: with the gate desk / cabin crew, pram tag | sledge: by the top / base station, sledge tag | bike: on the side / rear rail, rail tag | stale | supersede |
| (e1) genuine update, value restated (gated) | parking code "is X." / "is changed to Y ..." | chalet code "is X." / "is Y now, ..." | interval drinks code "is X." / "is Y now, ..." | (VALUE,): supersede | (VALUE,): supersede |
| (e2) genuine update, label noun (gated) | seat "is number X" / "is now number Y" | class "... switched to number Y" | row "... changed to number Y" | (number, VALUE): supersede | (number, LABEL, number): supersede |
| (f) unique (gated) | lounge voucher code | first aid card code | cast meet pass code | keep | keep |

Probes, 9 per scenario, in order a3x_old a3y_old e3x e3y e1 e2 f a3x_new a3y_new. Scoring is
`ctx_compress_r6_eval.score_recency` (unchanged).

## Deterministic pre-check (M, no model; `--precheck`, `precheck.json`)
**STOP rule.** If r14 drops a true old fact that r4 keeps, loses the stale fix on any existing set (recency, r8, r10,
r12, r13), differs from r4 on main, or differs from r13 byte for byte on recency / r8 / r10 / r12, the verdict is
REVERT(design, precheck) and there is no GHA run. Result: `stop = []` and `new_set_failures = []`.

Brief chars at 2810 (r4 / r13 / r14 / V offline):
- main 2485 / 2485 / 2485 / 2485 (r14 = r4 byte for byte; also on the capitalised briefs at every BUDGET, test).
- recency, r8, r10, r12: r14 = r13 byte for byte (test). V differs on r12 (2757-2775: V drops the r12 a old facts).
- r13: marina 2531 / 2756 / 2769 / 2644, hotel 2530 / 2774 / 2778 / 2650, festival 2549 / 2773 / 2793 / 2648. On r13,
  r14 keeps a3_old and fixes e3 (the r13 limits) and keeps everything else.
- r14: airport 2519 / 2488 / 2486 / 2651, ski 2510 / 2490 / 2488 / 2657, theatre 2518 / 2484 / 2495 / 2649.

## Dry prediction per arm (written before scoring; `dry-r14.json`, test_ctx_compress_r14.DRY)
Identically in all 3 r14 scenarios:
- **A:** a3x/a3y old present; e3x, e3y, e1, e2 STALE; f present; a3 newer facts absent.
- **B:** a3x/a3y old present and new absent (budget); e3x, e3y, e1, e2 CURRENT; f present.
- **R (r13):** a3x/a3y old ABSENT and new present; e3x, e3y STALE; e1, e2 CURRENT; f present.
- **O:** everything (updates BOTH).
- **V (offline only):** a3 old absent, e3 current.

On the dev / re-run sets, B holds every gated old fact and every current value, with no superseded value; on r13
B also holds a3_old and e3 CURRENT (report-only). A is STALE on every update.

Presence (A or B), over the 105 scored probes: 6 are absent from both (a3x_new, a3y_new, x 3).

Mock verdicts (fake server answering from its context, real CLI path, `mock-smoke.py` in the evidence dir):
- abstain -> **KEEP**. r14 a3 LOSS A 0 / B 0 / R 6. r14 e3 STALE A 6 / B 0 / R 6. STALE A 6 / B 0 on every set.
  r13 a3 LOSS B 0 and r13 e3 STALE A 3 / B 0 (report-only).
- invent -> KEEP. INVENTED on present probes A 0 / B 0 (R1 6).

**Expected outcome if Gemma answers from its context: KEEP.**

## Gates (`bench/ctx_compress_r14_verdict.py`)
Counts are per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips), pooled
over every scored r14 / r13 / r12 / recency / r8 / r10 probe (105 per block).

- **NULL:**
  - the r2 verdict is NULL;
  - for any set: scenarios != its 3 names, an incomplete job, a missing primary block, a request ERROR in a primary
    block, or per-block category counts not as frozen;
  - a scored probe has no presence entry;
  - a job lacks meta `r14_harness = ctx-compress-r14`;
  - there are no jobs.
- **PRIMARY metric:** r14 a3 old-fact LOSS (G1) and r14 e3 STALE (G1e); both must pass.
- **G1 r14_a3_loss (PRIMARY, G-a3):** old-fact LOSS (not RECALL) over r14 a3x_old and a3y_old (6 per block):
  max(LOSS_B) <= max(LOSS_A) + band.
- **G1e r14_e3_stale (PRIMARY, G-e3):** STALE over r14 e3x and e3y (6 per block): max(STALE_B) < min(STALE_A) - band.
- **G1b r13_fm_loss (dev):** old-fact LOSS over r13 a2x_old and a2y_old (6): B <= A + band.
- **G1c r12_fm_loss (dev):** the same over r12 a/b/c/d old (12).
- **G1d rerun_fm_loss:** the same over r8 c_old + r10 e1_old and e2_old (9).
- **G2 stale_fixed, on EVERY set:** max(STALE_B) < min(STALE_A) - band, for r14 e1 e2, r13 e1 e2, r12 e1 e2, recency
  bench badge, r8 a1 a2 and r10 a1 a2 (6 each).
- **G3 unique_kept, on every set:** min(RECALL_B) >= max(RECALL_A) - band, for r14 f, r13 f, r12 f, glaze, r8 d and
  r10 d (3 each).
- **G4 main_recall:** the frozen r2 verdict is KEEP (18 planted).
- **G5 r5_gates:** no r5 gate failure.
- **G6 no_new_invented_present:** max(INVENTED_B) <= max(INVENTED_A) + band, over scored probes present in A or B.
- **G7 latency:** main first-sentence p50, B <= A + max(|p50 A1 - p50 A2|, 5% of A).
- **KEEP** iff every gate passes; otherwise REVERT, listing every failed gate.
- **Report-only:** r13 a3 LOSS and r13 e3 STALE / CORRECT (the v2 limits, on the set they were found on; not gated
  because that set was seen when designing r14), r14 newer-fact recall, r14 e3 CORRECT, r12 (a) LOSS alone,
  INVENTED over absent-in-both probes, O1, R1, offline V.

## n and limits
- 3 held-out scenarios: 6 gated a3 old facts, 6 gated e3 updates and 6 normal updates per block. Smoke-scale, not
  powered.
- The set was written by the experimenter, who knew the r14 rule. Predicted r14 limits (not in the set, report-only
  by inference): a genuine update that also renames the label ("hook tag" -> "hook number") stays STALE; two
  different attributes with the same head and the same label phrase still merge; trailing prepositions enter the
  label ("the ticket on it is X" -> (ticket, on)); a value without a "my <subject> is" slot is never grouped.
- One model, CPU only, greedy decoding. Presence is a substring check.
- The newer facts of (a3) do not fit at 2810 for A or B (budget-bound), so they are report-only.

## Stop rule
- Exactly one scored run: the run triggered by the push that adds `.github/workflows/ctx-compress-r14.yml` to
  `ci/ctx-compress-r14`. `gh workflow run` is used only if that push starts no run.
- A NULL from infra (download, runner loss, job timeout) may be re-run once at the identical commit
  (`gh run rerun --failed`).
- Any other change (set, rule, gate, harness, budget, model) is a new experiment (r15).
- No edits to frozen files after the scored run starts.

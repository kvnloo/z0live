# C1-ctx-compress-r15 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow and this file are frozen by sha256 in
`ctx-compress-r15-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text only. Builds on
C1-ctx-compress-r14 (PR #179, run 46067246664): r14 (label tie-break on attribute key v2) was KEEP. **This is a
harness-only round: `brief_compress.py` is r14's byte for byte (sha256 5c54ae42..., test-checked).**

## What failed in earlier preregs, and what r15 changes
| round | outcome | what failed / was learned | how r15 handles it |
|---|---|---|---|
| r8 (r7 rule) | REVERT-r7 | slot-only supersession merged two different true facts (old-fact LOSS 3/block) | still-true pairs gated (G1) |
| r9 | REVERT (G7) | INVENTED gated over probes absent from both briefs; the mock assumed abstention | INVENTED gated only over probes present in A or B; invent-mode mock |
| r10 (r9 rule) | REVERT (G1) | a cue word on a different-attribute statement dropped the older true fact | unchanged rule (attribute key) |
| r11 | KEEP | attempt 1 NULL from a GGUF download (infra) | one infra re-run allowed |
| r12 (r11 rule) | REVERT (G1) | same head word, different attribute: false merge | r12 kept as a dev set, gated |
| r13 (v2) | KEEP | a3 merge / e3 stale limits, set written by the experimenter knowing the rule | r13 dev, gated |
| r14 (label) | KEEP | **every held-out set so far (r8-r14) was written by the experimenter, who knew the rule**; predicted, untested limits: renamed label stays stale, shared head + label merges, non-slot facts untouched, prepositions in the label | **r15: a held-out set whose text was written BLIND to the rule** |

## Question
Does r14 (B) hold against r4 (A) on a held-out set whose text was written blind to the rule? R = r13 report-only
rival, O = full brief report-only.

## Blind authoring (the novelty)
- Author: a free hosted LLM (an OpenAI-compatible router; the serving model varied per request and is logged locally).
  It got ONE neutral prompt per topic and nothing else: a casual multi-turn chat between a user and an assistant about
  (vet) booking care for a pet at a vet clinic, (car) renting a car for a road trip, (pottery) a weekly pottery class,
  in which the user states personal facts with short codes ("a capitalised word, a hyphen and a number"), including
  (1) three facts later genuinely updated in the user's own words (some reusing the original wording, some naming the
  same thing with a different noun / label), (2) two different facts with very similar wording that are both still
  true, (3) at least two facts stated in free form, not starting with "my X is", (4) one fact mentioned once, plus small
  talk, each fact in one sentence, and a KEY list at the end. The prompt never mentions compression, slots, cues,
  heads, labels or any rule, and the author never saw `brief_compress.py` or an earlier set.
- Acceptance (structural only, never the compressor): finish = stop, a KEY section, >= 8 distinct codes in User
  lines. The spec v1 attempts (3) and v2 attempts 2-4 for vet were rejected (truncated, empty, no codes in User lines);
  amendment 1 (before any conversion) accepted codes of any letter case, because the author wrote all-caps codes
  (PUP-102). Accepted: vet attempt 3, car attempt 2, pottery attempt 2. Prompts, raw responses and the attempt log are
  kept locally (sha256 of each accepted text in `ctx_compress_r15_blind.json`).
- Conversion by the experimenter (`ctx_compress_r15_blind.json`): every user fact sentence copied verbatim
  (test-checked locally against the raw text and its sha256); the first statement of each fact goes to the older block,
  every later statement (updates, the second fact of each pair) to the newer block, each block in blind order. Added
  only: probes, gold (from the blind text: newest valid value for updates, each value for pairs), categories, the r8
  topic noise (replacing the small talk, as in r8-r14), the harness acknowledgement ("Code X, got it ..."), r8.PAD and,
  on newer statements only, one neutral padding sentence (no slot, cue or digit; test-checked that the padded text has
  the same r14 key and cue flag as the bare sentence). Older exchanges <= 260 chars (9 car facts must fit 2810), noise
  between, newer > noise and <= 382 (test-checked).
- Dropped (logged in the data file): small-talk turns; the vet assistant summary listing every current code; the car
  pair-new probe (its sentence "The flight number for my trip is FLT-06 is FLT-07." names both codes, so its gold is
  ambiguous; the sentence stays in the chat). Items kept: 25 probes (9 updates, 3 pair-old, 2 pair-new, 11 static);
  dropped: 1 probe.

## Items, classified by surface form AFTER authoring
| scenario | update (u1-u3) | category / slot form | pair (p_old / p_new) | static (s) |
|---|---|---|---|---|
| vet | booking reference PUP-102 -> "I need to change my booking reference to PUP-205." | SAME, old only in slot | "My favorite vet tech is TECH-9." / "My go-to vet tech is TECH-12." | laser code, weekend tag, spare-key code (no slot) |
| vet | microchip number CHIP-77 -> "The microchip ID has been updated to CHIP-88." | RENAMED, old only | | |
| vet | clinic door code DOOR-44 -> "The access code is now DOOR-55." | RENAMED, old only | | |
| car | "My car rental booking reference is BOOK-01." -> "My updated car rental booking reference is BOOK-05." | SAME, both in slot (cue word inside the new subject) | "My flight number is FLT-06." / garbled pair sentence (probe dropped) | licence ID (slot), seat (no slot), dog (slot), weather (no slot), pizza (slot) |
| car | apartment door code DOOR-03 -> "My new apartment door code is DOOR-04." | SAME, both | | |
| car | credit card number CC-11 -> "My updated credit card number is CC-12." | SAME, both | | |
| pottery | "... booking reference ..., it's ClayRef-01." -> "I changed my booking, new reference is ClayRef-02." | RENAMED, no slot | parking spot Park-21 / bike rack Rack-22 (no slot) | clay smell, glaze, cat (no slot) |
| pottery | "My membership ID is MemberID-45." -> "I got a new membership card, it's MemberID-78." | RENAMED, old only | | |
| pottery | wheel-throwing Style-09 -> "I switched to hand-building, code Style-10." | SAME, no slot | | |

Counts: UPD_SAME 5, UPD_RENAMED 4 (slot form: both 3, old only 4, none 2), TWO_OLD 3, TWO_NEW 2, STATIC 11.

## Deterministic pre-check (M, no model; `--precheck`)
- **STOP rule:** if r14 loses the stale fix or a gated old fact that r4 keeps on any dev set (recency, r8, r12, r13,
  r14) or differs from r4 on main, the verdict is REVERT(design, precheck) and there is no GHA run. Result: `stop = []`.
- **r14 groups no blind update.** r14 attribute keys: 6 of 9 updates have no "my <subject> is" slot in the update
  sentence (key None: "I need to change my booking reference to ...", "The microchip ID has been updated to ...",
  "The access code is now ...", "I changed my booking, new reference is ...", "I got a new membership card, it's
  ...", "I switched to hand-building, code ..."); the other 3 have the cue word inside the slot subject ("my updated
  car rental booking reference is", "my new apartment door code is", "my updated credit card number is"), so the
  subject differs from the original's. Every r15 brief is byte-identical across r4 / r13 / r14 (test-checked):
  vet 2598, car 2756, pottery 2590 chars (full briefs 4830 / 5341 / 4830). Main: r14 = r4 byte for byte (2484-2485).

## Dry prediction per arm (written before scoring; `dry-r15.json`, test_ctx_compress_r15.DRY)
In all 3 r15 scenarios:
- **A, B and R (identical briefs):** u1-u3 STALE (the updates do not fit; the old values do); p_old present; every
  static fact present; p_new absent.
- **O:** updates BOTH, everything else present.
- Presence (A or B) over the 103 scored probes: 11 absent from both (9 updates + 2 pair-new, all r15).
- On the dev sets B holds every gated old fact and every current value (same code as r14's KEEP run).

Mock verdicts (fake server answering from its context, real CLI path, `mock-smoke.py` in the evidence dir):
abstain -> **KEEP** (r15 update STALE A 9 / B 9 / R 9, pair LOSS 0 / 0 / 0, static recall 11 / 11); invent -> KEEP.

**Expected outcome: KEEP in the narrow sense that B is no worse than r4, because B's context equals A's; the
interesting measurement is that r14's update fix (G1e in r14) does not transfer: 0 of 9 blind updates are superseded.**
A and B blocks differ only by run-to-run noise (band).

## Arms (all via `tiers.snapshot`, 6000 cap, budget 2810)
Harness `bench/ctx_compress_r15_eval.py` over the frozen `ctx_compress_r10_eval` / r2 harness. Model gemma-4-E2B-it
Q4_K_M, llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy seed 42, fresh llama-server per block.
A = `bench/brief_compress_r4.py`, B = `brief_compress.py` (r14), R = `bench/brief_compress_r13.py` (report-only,
block R1 on r15), O = full brief (report-only, O1 on main and r15).

Jobs (24 = 3 scenarios x 8; deadlines as r14):
- main: lo (probes 0-3), hi (4-7), B1 A1 B2 A2 O1, 2580 s.
- r15: hx (vet / pottery 0-3, car 0-4), hy (vet / pottery 4-7, car 5-8), B1 A1 B2 A2 O1 R1, 3000 s.
- dev, B1 A1 B2 A2, gated probes only: dw r14 0-6 (3000 s), dx r13 0-4 (3000 s), dy recency 0-2 (1400 s) + r8 0,1,2,5
  (1500 s), dz r12 0-6 (3000 s). r10 is not re-run (job cap; r10's gates held in r11-r14 with the same B code).

## Gates (`bench/ctx_compress_r15_verdict.py`)
Counts per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips), pooled over
every scored r15 / r14 / r13 / r12 / recency / r8 probe (103 per block).
- **NULL:** r2 verdict NULL; for any set: scenarios != its 3 names, incomplete job, missing primary block, request ERROR
  in a primary block, per-block category counts not as frozen; a scored probe with no presence entry; a job without
  meta `r15_harness = ctx-compress-r15`; no jobs.
- **G1 r15_two_true_loss (PRIMARY):** old-fact LOSS (not RECALL) over r15 TWO_OLD (3): max(B) <= max(A) + band.
- **G1e r15_update_stale (PRIMARY):** STALE over the union of r15 updates (9): max(B) <= max(A) + band (no worse than
  r4). Reported split by UPD_SAME / UPD_RENAMED and by slot form, with CORRECT counts.
- **G1s r15_static_recall:** RECALL over r15 STATIC (11): min(B) >= max(A) - band.
- **G1b r14_a3_loss (dev):** LOSS over r14 a3x_old a3y_old (6): B <= A + band.
- **G1c dev_fm_loss:** LOSS over r13 a2x/a2y old + r12 a-d old + r8 c_old (21): B <= A + band.
- **G2 stale_fixed, every dev set:** max(STALE_B) < min(STALE_A) - band for r14 e3x e3y e1 e2 (12), r13 e1 e2, r12 e1
  e2, recency bench badge, r8 a1 a2 (6 each).
- **G3 unique_kept, every dev set:** min(RECALL_B) >= max(RECALL_A) - band for r14 f, r13 f, r12 f, glaze, r8 d (3).
- **G4** r2 verdict KEEP (18 planted). **G5** no r5 gate failure. **G6** max(INVENTED_B) <= max(INVENTED_A) + band over
  scored probes present in A or B. **G7** main first-sentence p50 B <= A + max(|p50 A1 - p50 A2|, 5% of A).
- **KEEP** iff every gate passes; otherwise REVERT, listing every failed gate.
- Report-only: r15 STALE / CORRECT splits, r15 pair-new recall, O1, R1, INVENTED over absent-in-both probes.

## n and limits
- 3 blind scenarios: 9 updates, 3 pair-old facts, 11 static facts per block. Smoke-scale, not powered.
- The blind author is one free hosted LLM per topic (model varied); its text is unnatural in places (codes tacked onto
  preferences, one garbled pair sentence). Most codes are all-caps (PUP-102), which the frozen scorer's INVENTED pattern
  (`[A-Z][a-z]+-\d+`) does not match: a wrong all-caps code scores OFF_FORMAT, not INVENTED. LOSS / STALE are unaffected.
- Classification (SAME / RENAMED, slot form) is the experimenter's, after authoring, from surface form; listed above.
- Because B = A on every r15 brief, G1 / G1e / G1s can only fail through run-to-run noise beyond the band.
- One model, CPU only, greedy decoding. Presence is a substring check.

## Stop rule
- Exactly one scored run: the run triggered by the push that adds `.github/workflows/ctx-compress-r15.yml` to
  `ci/ctx-compress-r15`. `gh workflow run` is used only if that push starts no run.
- A NULL from infra (download, runner loss, job timeout) may be re-run once at the identical commit
  (`gh run rerun --failed`).
- Any other change (set, rule, gate, harness, budget, model) is a new experiment (r16).
- No edits to frozen files after the scored run starts.

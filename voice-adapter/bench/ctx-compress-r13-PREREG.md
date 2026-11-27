# C1-ctx-compress-r13 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow and this file are frozen by sha256 in
`ctx-compress-r13-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text only. Builds on
C1-ctx-compress-r12 (PR #177, run 46048241576): r11 reverted on G1 only, and all 3 of its old-fact losses came from
item (a), a newer cued statement with the same predicate head word but a different attribute.

## Question
Does attribute key v2 (supersede only with an update cue AND the same (slot subject, first two non-filler predicate
words)) remove r11's same-head false merge without losing the stale-value fix?

## The ONE change (production)
`voice-adapter/brief_compress.py`: `_attribute` returns (slot subject, *`_key`(predicate)), where `_key` takes the
first `KEY_WORDS = 2` words of the predicate after the copula that are not in FILLER, with every CUE match removed. A
word holding a digit is VALUE and ends the key; an empty predicate gives (VALUE,). FILLER, WORD, CUE, VALUE and SLOT
are r11's, unchanged (test-checked). The first key word is r11's head (test-checked on every r12 / r13 statement), so
v2 only splits r11 attribute groups; it never merges new ones. r11 is kept as `bench/brief_compress_r11.py`, a byte copy
of brief_compress.py at 3dcf56c (sha256 f0233b21...). Older test modules that pinned r11 behaviour
(`test_brief_compress_r11`, `test_ctx_compress_r12`) now import or patch in that byte copy.

**Primary candidate: v2 = head + next content word. Chosen before any scoring and not switched.** The rival "same
value shape" (both values the same token class with the same label noun) is recorded here and NOT run: an R2 arm
would add a block to every probe job, and the r12 / r13 items do not separate it from v2 cheaply. Arm R is r11.

## Arms (all via `tiers.snapshot`, 6000 cap, budget 2810)
Harness `bench/ctx_compress_r13_eval.py` over the frozen `ctx_compress_r10_eval` / r2 harness. Model gemma-4-E2B-it
Q4_K_M, llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy seed 42, fresh llama-server per block.
- A = `bench/brief_compress_r4.py` (r4, baseline).
- B = `brief_compress.py` (r13, attribute key v2).
- R = `bench/brief_compress_r11.py` (r11), report-only rival, block R1 on r13.
- O = full brief, report-only, block O1 on main and r13.

Blocks and jobs (21 = 3 scenarios x 7):
- main: B1 A1 B2 A2 O1, `lo` probes 0-3, `hi` 4-7.
- r13 (held-out): B1 A1 B2 A2 O1 R1, `hx` probes 0-4, `hy` 5-9.
- Dev and re-run sets, B1 A1 B2 A2, gated probes only:
  - `dx`: r12 probes 0-6 (a-d old, e1, e2, f).
  - `dy`: recency 0-2 (bench, badge, glaze), then r8 0,1,2,5 (a1, a2, d, c_old).
  - `dz`: r10 0-4 (a1, a2, d, e1_old, e2_old).
  They are re-run because B's code changed. The r12 newer-fact probes and the r8 b / c_new and r10 *_new probes are
  report-only in earlier rounds and are not re-run, to keep the matrix at 21 jobs.

Deadlines: 2580 s (main, r10), 3000 s (r13, r12), and 1400 s + 1500 s (recency, then r8). Step timeout 55 min, job
timeout 65 min.

## The r13 set (NEW, held out: marina, hotel, festival)
Stable brief and call turns are the r2 scenarios' own (as r8 / r10 / r12). Per scenario:
- 7 older facts (295-319 chars);
- 8 topic noise turns (4 exchanges, 354-359 chars);
- 6 newer facts (376-382 chars).

Code families: Vega Rigel Deneb Altair Sirius Castor Lyra Orion Spica Antares Pollux Mira Polaris. No earlier set or
stable brief uses them (test-checked). v2 key per statement (`ctx_compress_r13_eval.keys`, test-checked):

| item | marina | hotel | festival | r11 | v2 prediction |
|---|---|---|---|---|---|
| (a2) x: same head, different 2nd word, different attribute (gated) | boat: moored at / now moored for (berth tag vs winter storage ref) | suitcase: left at / now left with (claim tag vs shuttle ticket) | camper van: parked by / now parked for (parking pass vs exit pass) | heads coincide + cue: drops old | keys differ: keep both |
| (a2) y (gated) | dinghy: stored in / stored on | rental car: parked in / parked for | wristband: linked weekend / linked with | drops old | keep both |
| (a3) same head AND same 2nd word, different attribute (**predicted v2 limit**, report-only) | paddleboard: booked for (tour ref / repair ticket) | room key: linked with (spa ref / breakfast plan code) | friend group: registered for (group ref / workshop code) | drops old | keys coincide + cue: **drops old (false merge)** |
| (e1) genuine update, value restated with other words around it (gated) | mooring fee ref "is X." / "is changed to Y from this month" | wifi code "is X." / "is Y now, reception reset" | locker code "is X." / "is Y now, the stewards" | (VALUE) both: supersede | (VALUE,) both: supersede, stale fixed |
| (e2) genuine update, label noun before the value (gated) | fuel card "is number X" / "is now number Y" | room "is number X" / "is changed to number Y" | shuttle seat "is number X" / "is switched to number Y" | head "number": supersede | (number, VALUE) both: supersede |
| (e3) genuine update, same head, different 2nd word (**predicted v2 limit**, report-only) | tender: on north jetty / now on south jetty | bike: in front rack / now in back rack | flag: on left pole / now on right pole | head coincides: supersede, fixed | keys differ: **keeps the stale value** |
| (f) unique (gated) | harbour gate fob code | minibar card code | first aid badge code | keep | keep |

Probes, 10 per scenario, in order a2x_old a2y_old e1 e2 f a3_old e3 a2x_new a2y_new a3_new. Scoring is
`ctx_compress_r6_eval.score_recency` (unchanged).

## Deterministic pre-check (M, no model; `--precheck`, `precheck.json`)
**STOP rule.** If r13 drops a true old fact that r4 keeps, or loses the stale fix on any existing set (recency, r8,
r10, r12), the verdict is REVERT(design, precheck) and there is no GHA run. Result: `stop = []` and
`new_set_failures = []`.

Brief chars at 2810 (r4 / r11 / r13):
- main 2485 / 2485 / 2484, and r13 = r4 byte for byte. Also equal on the capitalised briefs at every BUDGET (test).
- recency, r8 and r10: r13 = r11 byte for byte (test).
- r12: campsite 2519 / 2757 / 2679, coworking 2531 / 2775 / 2694, leisure 2524 / 2773 / 2689.
- r13: marina 2531 / 2644 / 2756, hotel 2530 / 2650 / 2774, festival 2549 / 2648 / 2773.

On r12, r13 keeps a_old (r11 dropped it in all 3 scenarios), keeps b-d old, fixes e1 and e2, and keeps f.

## Dry prediction per arm (written before scoring; `dry-r13.json`, test_ctx_compress_r13.DRY)
The context holds the following, identically in all 3 r13 scenarios:
- **A:** every old fact; e1, e2 and e3 STALE; no newer fact.
- **B:** a2x/a2y old present and new absent (budget); **a3_old ABSENT and a3_new present** (limit); e1 and e2
  CURRENT; **e3 STALE** (limit); f present.
- **R:** a2x/a2y/a3 old ABSENT and new present; e1, e2 and e3 CURRENT; f present.
- **O:** everything.

On the dev / re-run sets, B holds every gated old fact and every current value, with no superseded value. A is STALE
on every update.

Presence (A or B), over the 87 scored probes: 9 are absent from both (a2x_new, a2y_new and e3, x 3).

Mock verdicts (fake server answering from its context, real CLI path, `mock-smoke.py` in the evidence dir):
- abstain -> **KEEP**. r13 FM LOSS A 0 / B 0 / R 6. r12 FM LOSS A 0 / B 0. STALE A 6 / B 0 on every set. Unique
  3/3. a3 LOSS B 3 (report-only). e3 STALE A 3 / B 3 / R 0.
- invent -> KEEP. INVENTED on present probes A 3 / B 3 (parity).

**Expected outcome if Gemma answers from its context: KEEP.** The two predicted v2 limits (a3, e3) are report-only by
design and are expected to show B a3 LOSS 3 and B e3 STALE 3.

## Gates (`bench/ctx_compress_r13_verdict.py`)
Counts are per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips), pooled
over every scored r13 / r12 / recency / r8 / r10 probe (87 per block).

- **NULL:**
  - the r2 verdict is NULL;
  - for any set: scenarios != its 3 names, an incomplete job, a missing primary block, a request ERROR in a primary
    block, or per-block category counts not as frozen;
  - a scored probe has no presence entry;
  - a job lacks meta `r13_harness = ctx-compress-r13`;
  - there are no jobs.
- **G1 r13_fm_loss (PRIMARY):** old-fact LOSS (not RECALL) over r13 a2x_old and a2y_old (6 per block):
  max(LOSS_B) <= max(LOSS_A) + band. a3 is excluded (report-only).
- **G1b r12_fm_loss (dev):** the same over r12 a/b/c/d old (12).
- **G1c rerun_fm_loss:** the same over r8 c_old + r10 e1_old and e2_old (9). This gate is added because B changed; it
  only makes the test stricter.
- **G2 stale_fixed, on EVERY set:** max(STALE_B) < min(STALE_A) - band, for r13 e1 e2, r12 e1 e2, recency bench badge,
  r8 a1 a2 and r10 a1 a2 (6 each).
- **G3 unique_kept, on every set:** min(RECALL_B) >= max(RECALL_A) - band, for r13 f, r12 f, glaze, r8 d and r10 d
  (3 each).
- **G4 main_recall:** the frozen r2 verdict is KEEP (18 planted).
- **G5 r5_gates:** no r5 gate failure.
- **G6 no_new_invented_present:** max(INVENTED_B) <= max(INVENTED_A) + band, over scored probes present in A or B.
- **G7 latency:** main first-sentence p50, B <= A + max(|p50 A1 - p50 A2|, 5% of A).
- **KEEP** iff every gate passes; otherwise REVERT, listing every failed gate.
- **Report-only:** r13 a3 LOSS, r13 e3 STALE / CORRECT, r13 newer-fact recall, r12 (a) LOSS alone, INVENTED over
  absent-in-both probes, O1, R1.

## n and limits
- 3 held-out scenarios: 6 gated false-merge old facts and 6 updates per block, plus 3 + 3 report-only limit items.
- The set was written by the experimenter, who knew the v2 rule. (a3) and (e3) probe its known limits on purpose.
- One model, CPU only, greedy decoding. Presence is a substring check.
- The newer facts of (a2) do not fit at 2810 for A or B (budget-bound, as in r11 / r12), so they are report-only.
- Smoke-scale, not powered.

## Stop rule
- Exactly one scored run: the run triggered by the push that adds `.github/workflows/ctx-compress-r13.yml` to
  `ci/ctx-compress-r13`. `gh workflow run` is used only if that push starts no run.
- A NULL from infra (download, runner loss, job timeout) may be re-run once at the identical commit
  (`gh run rerun --failed`).
- Any other change (set, rule, gate, harness, budget, model) is a new experiment (r14).
- No edits to frozen files after the scored run starts.

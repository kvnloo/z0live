# C1-ctx-sem-r3 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow, the dry prediction, the oracle bound, the
CPU-key replay and this file are frozen by sha256 in `ctx-compress-sem-r3-FREEZE.sha256` (checked in every job and in
the verdict job). Synthetic text only. Builds on C1-ctx-sem-r2 (PR #185, run 46098403361, head 4306698b, REVERT:
failed G1 only). `brief_compress.py` (r14), `brief_compress_sem.py` (sem-r1) and `brief_compress_sem2.py` (sem-r2) are
untouched (test-checked by sha256).

## Question
sem-r2's hard static reservation left 4/9 blind r15 updates with neither value in the brief (CORRECT 4/9). Is the
binding constraint the budget rather than the keyer, so that letting a superseding line take the older line's slot
(trimmed to fit) recovers the updates without displacing statics, on r15 and on a second, larger blind set?

## ONE change vs sem-r2
`voice-adapter/brief_compress_sem3.py`. Keys, collision rule, partner rule, judge prompt, key prompt, cache, arrival
simulation and fallback are sem-r2's own functions (imported, unchanged). Only the selection after the verdicts changes:
- **removed:** the hard reservation (every static fact r4 keeps reserved before the greedy fill).
- **added (soft budget rule):** a superseding line (a candidate whose REPLACE verdict dropped at least one older
  statement) is emitted trimmed and placed at the position of the oldest statement it (transitively) replaced; its size
  may not exceed that older exchange's size + SLACK (8 chars). Trim levels, first that fits: (1) the value-bearing
  sentence (sem-r1 `statement`: first sentence holding a word with a digit; padding and later sentences dropped) plus
  the reply lines unchanged; (2) that sentence alone; (3) the comma / semicolon / colon clause of that sentence holding
  its last word with a digit. If no level fits (or the slot is already taken), the line stays at its own position and
  size. Then plain r4 order (fact exchanges shortest first, equal sizes newest first, filler tail), no reservation.
- Tests first: `test_ctx_compress_sem_r3.py`; the two behaviour tests (`SoftBudget.test_superseding_line_takes_older_slot_trimmed`,
  `SoftBudget.test_oracle_capacity_on_both_blind_sets`) fail when run against sem-r2's selection
  (`SEM3_UNDER_TEST=sem2`: 2 failures, "'Oak-22' not found" and "('r15', 'r15-vet', 'u1')") and pass on sem-r3.

## Second blind set (new scored input): `bench/ctx_blind_v2.json`
Six chats written by free hosted models (gpt-oss-120b / gpt-oss-20b, alternating) from the r15 neutral spec with the
topic substituted (dentist, apartment move-in, gym, school pickup, home internet, book club); the authors never saw the
compressor, a rule or an earlier set. Converted deterministically from each text's own KEY section (no model consulted):
UPD 17 (UPD_SAME 8 / UPD_RENAMED 9, a report-only word-overlap split), TWO_OLD 6, TWO_NEW 6, STATIC 21; 5 KEY items
excluded (no code). Every fact `text` is a whole verbatim User line; the raw texts stay outside the repo (sha256 per
scenario in the file). Harness = r15's: per v2 scenario i the stable brief / call turns of r2 scenario i % 3, the first
statement of every fact (padded to 260), the topic noise, then every later statement (padded to 382 with r15's note).
Asks are generated from the KEY labels by rule (updates "What is my <label> now?", others quote the line with the code
blanked) + r15's "Please give just the code.". Presence checks use a whole-code match (v2 has codes that are prefixes
of other codes: GENRE-202 / GENRE-2022, LOC-303 / LOC-3032, LIST-505 / LIST-5052, Time-300 / Time-300B); the frozen
scorer already matches whole codes.

## What failed in sem-r1 / sem-r2, and what r3 changes
| item | sem-r1 (run 46088778796) | sem-r2 (run 46098403361) | r3 change | expected (I) |
|---|---|---|---|---|
| G1 pair-old LOSS | 2 vs 0 (vet near-synonym key merge; car garbled line) | 1 vs 0 (car garbled line "FLT-06 is FLT-07" judged REPLACE; model OFF_FORMAT) | none to keys / judge; the garbled line now sits trimmed in FLT-06's own slot | NOT fixed on r15 (same verdict); v2 adds 6 pairs, two of them read almost like updates (Time-300 / Time-300B, READ-808 / READ-909) |
| G1e update CORRECT | 7/9 | 4/9 (reservation: 4 updates with neither value) | no reservation; trimmed line in the older slot | r15 current in brief 8/9 (CPU-key replay) |
| G1s statics | 10/11 (displaced by a longer superseding line) | 11/11 (by reservation) | trimmed line never longer than the slot + 8 | 11/11 r15 (replay), 21/21 v2 (oracle) |
| G8 latency | 11.6 s per brief (batched pass) | G8a 0.001 s (cached on arrival) | unchanged | G8a p50 < 0.05 s |
| prereg weakness | blind n = 3 | blind n = 3, one garbled sentence decided G1 | blind v2 adds 6 scenarios; gates pooled | - |

## Arms, blocks, matrix
Model gemma-4-E2B-it Q4_K_M (sha 740185b2..., pinned), llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy seed 42, no prompt
cache, fresh llama-server per block. A = r4 byte copy; B = sem-r3; O = full brief (main only, report-only). No R arm
(sem-r1 is measured). Blocks: B A B A O (main), B A B A (r15, v2, dev). 36 jobs: 3 scenarios x {lo, hi (main), hx, hy
(r15), dw (r14), dx (r13), dy (recency + r8), dz (r12)} + 6 v2 scenarios x {vx, vy}. Job timeout 50 min (3000 s),
harness deadline 2400 s (dy 1150 + 1250).

## Gates (`bench/ctx_compress_sem_r3_verdict.py`), sem-r2's gates with the blind ones pooled
**Pooled rule:** for each primary block label L (A1 B1 A2 B2) and blind category, count_L = count_L(r15) + count_L(v2);
gates compare pooled counts; per-set counts are reported (not gated). band = max(A1 vs A2 flips, B1 vs B2 flips) pooled
over every scored probe of r15 / v2 / r14 / r13 / r12 / recency / r8 (sem-r2 band was 0).
n per block: UPD 9 + 17 = 26, TWO_OLD 3 + 6 = 9, STATIC 11 + 21 = 32, TWO_NEW 2 + 6 (report).
- **NULL:** as sem-r2, per set (v2 names, counts, complete jobs, A1 B1 A2 B2 present, no ERROR), sem_harness meta
  `ctx-compress-sem-r3`, B rows with `sem` / `in_brief` / `in_brief_ablation`, presence entries, >= 1 B sample.
- **G1 (PRIMARY) blind_two_true_loss:** max(LOSS_B) <= max(LOSS_A) + band, pooled TWO_OLD.
- **G1e (PRIMARY) blind_update_fixed:** max(STALE_B) <= min(STALE_A) - 3 - band, pooled UPD, and min(CORRECT_B) >= 3
  (sem-r2's thresholds unchanged; with n = 26 this is a weak bar, the per-set counts are reported next to it).
- **G1s blind_static_recall:** min(RECALL_B) >= max(RECALL_A) - band, pooled STATIC.
- **G1b, G1c, G2, G3, G4, G5, G6, G7:** sem-r2's, unchanged.
- **G8a brief_latency:** p50 brief-time added wall (warm cache) per distinct (job, B block, brief) <= 3.0 s, no fallback,
  no model-call error in any B block. **G8b** (report-only): per-new-statement arrival cost p50 / p95 / max.
- KEEP iff every gate passes; otherwise REVERT, listing every failed gate. Report-only: per-set gate counts, SAME /
  RENAMED split, TWO_NEW, presence for B / B-nojudge / B-sem2 (sem-r2's selection on the same cache), trim telemetry.

## Deterministic checks and dry prediction (written before the scored run)
- `ctx_compress_sem_r3_oracle_bound.json` (perfect keys + judge): current in brief B 26/26 (r15 9/9, v2 17/17),
  superseded 0; pair-old 9/9; statics 32/32. sem-r2 selection: 17/26 (r15 6/9, v2 11/17). A: 0/26 current.
- `ctx_compress_sem_r3_replay_cpu_keys.json`: sem-r2's CI keys and verdicts (338 keys, 155 verdicts, 0 missing)
  replayed through sem-r3 on main + r15 + dev: STOP list empty (no dev old fact r4 holds is dropped); main B == A byte
  for byte; every gated dev stale probe holds the current value and no superseded one; r15 current 8/9 (vet u2 keys do
  not collide), superseded 1/9, pair-old 3/3 in brief (car FLT-06 only inside the trimmed garbled line in FLT-06's
  slot), statics 11/11. The replay of sem-r2's own selection reproduces sem-r2's CI presence (4/9, 1/9) exactly. v2 has
  no CPU keys (never sent to a model before scoring).
- Mock smoke (`mock-smoke.py`, evidence dir: fake server, heuristic keyer / judge) runs the real CLI path for all 36
  jobs; both modes give non-NULL verdicts (REVERT from the heuristic stand-ins; pipeline check only).
- **Dry prediction (I): REVERT, failed G1 only.** r15: STALE 1, CORRECT 6-8, pair-old LOSS 1 (car), statics 11/11.
  v2: STALE 1-5 of 17, CORRECT 10-15, pair-old LOSS 1-2 of 6 (Time-300 / Time-300B and READ-808 / READ-909 likely
  judged REPLACE), statics 21/21. Pooled: G1 LOSS B 2-3 vs A 0 (FAIL unless band >= 2), G1e pass, G1s pass, dev gates
  pass, G8a p50 < 0.05 s, G8b per new statement p50 1.4-2.0 s. Confidence moderate; the alternative is KEEP (model
  answers FLT-06 from the trimmed line and the v2 pairs survive).

## Limits
- One model, CPU, greedy. Blind n = 9 scenarios (26 / 9 / 32 per block), still smoke-scale; band 0 would let one flip
  decide a gate.
- KEY_PROMPT / JUDGE_PROMPT untouched since sem-r2 (never tuned on blind text). v2 asks are rule-generated and some are
  terse ("What is my dentist now?"); A and B get the same asks.
- Trimming rewrites a line (not a verbatim subset of the brief any more); the asker label is kept.
- The arrival simulation feeds a conversation's statements just before the brief (same caveat as sem-r2).
- `compress_brief_sem3` is not wired into tiers.snapshot.

## Stop rule
- Exactly one scored run: the run started by the push that adds `.github/workflows/ctx-compress-sem-r3.yml` to
  `ci/ctx-sem-r3`; `gh workflow run` only if that push starts no run.
- A NULL from infra (GGUF download, runner loss, job timeout) may be re-run once at the identical commit
  (`gh run rerun --failed`). Infra failing twice => NULL(infra).
- Any other change (set, prompt, rule, gate, harness, budget, model, slack) is a new experiment.
- No edits to frozen files after the scored run starts.

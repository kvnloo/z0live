# C1-ctx-sem-r5 judge oracle: PREREG (written before any model call of this experiment)

Not a scored run. Question: does ONE changed collision-judge prompt fix the two clear false-ALSO verdicts of sem-r3
(run 46105850056) without turning any still-true pair into REPLACE?

## The one change
`JUDGE_PROMPT_R5` (`voice-adapter/brief_compress_sem5.py`, frozen by sha256 in `ctx-compress-sem-r5-oracle-FREEZE.sha256`)
replaces sem-r2's `JUDGE_PROMPT`. Rule: REPLACE when the NEWER statement names the same slot of the same subject with a
new value (moved / switched / changed / now / new count); ALSO only when both values can hold at once (two different
things, people or purposes). Four few-shot examples (2 REPLACE, 2 ALSO) are modelled on dev sets r8 and r12 templates
with fresh code values; none comes from v2, v3 or r15 text (a unit test checks that no example line is a recorded pair).
Output format, `parse_judge`, model (gemma-4-E2B-it Q4_K_M, rev 0314792d), llama.cpp b12527, CPU, temp 0, seed 42,
max 4 tokens, server flags: unchanged.

## Pairs (`ctx_compress_sem_r5_pairs.json`, frozen)
- 188 distinct (older, newer) pairs judged in the sem-r3 B1 rows of every set (main, recency, r8, r12, r13, r14, r15, v2),
  each with its sem-r3 verdict (all recorded repeats agree), and a gold class per its set's probes: older value in the
  `superseded` list of a probe whose `current` is in the newer statement = update (REPLACE correct); older statement
  holds a still-true probe value = still-true pair (ALSO correct); else noise (reported).
- Ambiguous, report-only, no gold: v2-pickup Time-300 / Time-300B, r15-car FLT-06 / FLT-07 (AUDIT.md of sem-r4).
- Plus 10 EXTRA pairs from the sem-r4 oracle (never judged by sem-r3): includes the still-true risks vet tech
  TECH-9/TECH-12, ISP account Aspen-99/Birch-66, r8 bike tag / bike rack code, r12 locker tag / keypad code, r12 pitch
  number / pitch tag. Extras have no recorded verdict; they count in the false-REPLACE test only.

## Procedure
One llama-server, pairs in file order, three passes: `old1` (sem-r2 prompt), `new` (R5), `old2` (sem-r2 again).
- A/A control: `old1` must equal the sem-r3 recorded verdict on all 188 recorded pairs; and `old1` == `old2` on all 198.
  A/A flips = count of disagreements (both comparisons summed). Noise band = that count.
- fixed = clear update pairs whose sem-r3 verdict was ALSO (expected 2: v2-gym Tango-303/311, v2-pickup Route-12/15) and
  R5 says REPLACE.
- kept = clear update pairs sem-r3 had as REPLACE and R5 keeps as REPLACE; lost = those R5 does not (report-only).
- new false-REPLACE = clear still-true pairs (gold ALSO, incl. extras) where R5 says REPLACE and `old1` did not.
  Reported per set. Noise pairs and ambiguous pairs: reported, never gated. Unparsable reply = no REPLACE.

## Stop rule (frozen)
Proceed to a scored run ONLY IF fixed >= 1 AND new false-REPLACE == 0 AND A/A flips == 0.
Otherwise the verdict is NULL(oracle-stop): no scored run, no PR. Lost updates (`lost`) do not gate but are reported;
a scored run would additionally need `lost` reviewed first. Nothing is reclassified after the model has run.

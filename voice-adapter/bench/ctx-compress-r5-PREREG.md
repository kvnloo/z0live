# C1-ctx-compress-r5 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict/gate scripts frozen by sha256 in `ctx-compress-r5-FREEZE.sha256`
(checked in-workflow). Synthetic text only. Builds on C1-ctx-compress-r4 (PR #169, run 45918447833).

## Question
r4 passed the r2 recall rule (planted recall A1 A2 B1 B2 18/18, A/A 0) but the r3 SKIP gate failed: B had 1 SKIP per B
block vs A 0, both on the UNPLANTED `ticket` probe in workshop-3, where A answered "I do not have your gallery ticket
code." (scored OFF_FORMAT, not SKIP). On an unplanted probe the right answer is to decline; SKIP and a text abstention are
both declines. With a gate that counts SKIP/SUPERSEDED/INVENTED on planted probes only and treats SKIP and text abstention
as equivalent on unplanted probes, does the r4 compressor (unchanged) keep planted recall at the full-brief level without
more wrong or invented answers than the full brief?

## The ONE change (harness rule only)
New gates script `bench/ctx_compress_r5_gates.py` replaces `bench/ctx_compress_r3_gates.py` as the gate:
- Planted probes (probe has a current value): for each of SKIP, SUPERSEDED, INVENTED, each B block count (summed over
  jobs) must be <= max(A1, A2) count.
- Unplanted probes (no current value): an answer is ABSTAIN if it is SKIP or matches the frozen abstention regex
  ("I do not have / don't know", "I'm not sure/certain/aware", "I can't find", "not in the context/notes/brief",
  "no record of"); any other non-ERROR answer is WRONG (INVENTED or non-abstaining OFF_FORMAT). Gates: each B block WRONG
  <= max(A1, A2) WRONG, and each B block INVENTED <= max(A1, A2) INVENTED. SKIP vs text abstention counts are report-only.
- Blocks matched by label, so block order does not matter.
- Unit tests `test_ctx_compress_r5_gates.py` (10 tests) on a committed fixture of r4's 6 job JSONs (scoring fields only,
  `fixtures/ctx_compress_r4_jobs.json`): r4 data gives r2 KEEP, r3 gates REVERT, r5 gates KEEP; synthetic B SKIP or
  SUPERSEDED on a planted probe -> REVERT; synthetic B INVENTED or non-abstaining wrong answer on an unplanted probe ->
  REVERT; all-SKIP on unplanted ticket -> KEEP; NULL/REVERT pass through; abstention regex positive/negative cases.

Production code unchanged: `voice-adapter/brief_compress.py` = r4, sha256 5a88bc8b... (byte-identical, frozen).

## Fresh ordering (greedy decoding means a same-order re-run is a byte re-read)
Block order B,A,B,A,O (labels B1 A1 B2 A2 O1); r4 was A,B,A,B,O. Uses the eval script's existing `--blocks` flag; no
frozen harness file changed. Fresh llama-server per block, so order changes only server start sequence and runner
thermal/timing state; replies may still be byte-identical to r4 (greedy, seed 42). That is a valid outcome, not NULL.

## Harness: frozen r2 harness, unchanged
`bench/ctx_compress_r2_eval.py` (e5e0ef95...), `context_eval.py` (d9abb7b6...), `lanespeed.py` (02bd44d6...), verdict
`bench/ctx_compress_r2_verdict.py` (c44d4ca0...); 3 workshop scenarios x 8 probes (18 planted), 6-job matrix (scenario x
probe half), fresh llama-server per block, greedy, seed 42, max 120 tokens, thinking off, cache_prompt false,
gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 ubuntu arm64 CPU, ubuntu-24.04-arm, deadline 2580 s, same
timeouts as r4. A = full brief, B = production compress_brief (r4), O = pre-r2 compressor (report-only).

## Frozen rule (= r2 verdict + r5 gates)
- KEEP iff r2 verdict KEEP (min(B1, B2) >= max(A1, A2) - band, band = A/A flips A1 vs A2) AND all r5 gates pass.
- Any failed r5 gate -> REVERT.
- NULL: missing or incomplete jobs, a missing primary block, a request error in a primary block, wrong scenarios,
  != 18 planted per block (all from the r2 verdict script, unchanged).
- Report-only: O1 recall and discrimination (O1 < min A); first_sentence p50 pooled per arm; B/B flips; unplanted SKIP vs
  text-abstention split; r3 gates on the same data.

## Expectation (I, stated before scoring)
Offline re-gate of r4's job JSONs with the r5 gates (M, report-only, not the verdict): KEEP, no gate failures; planted
SKIP/SUPERSEDED/INVENTED 0 in every block; unplanted per block: A1/A2 ABSTAIN 3 WRONG 3, B1/B2 ABSTAIN 3 (1 SKIP + 2 text)
WRONG 3, INVENTED 0. The 3 WRONG per block in both arms are the unplanted `wifi` probe answered with the studio code.
If the fresh-order run reproduces r4 replies, expect KEEP. A REVERT would need B to lose planted recall beyond the band,
skip/supersede/invent on a planted probe, or answer an unplanted probe wrongly more often than A.

## What the r4 prereg got wrong and how r5 changes it
1. The r3 gates counted SKIP over ALL probes, so an abstention on an unplanted probe (the correct behaviour) counted as
   a regression. r5 counts SKIP/SUPERSEDED/INVENTED on planted probes only.
2. The scorer grades a text abstention ("I do not have ...") as OFF_FORMAT and a bare "SKIP" as SKIP, so the r3 gate
   compared two equivalent declines as different. r5 treats them as one ABSTAIN class on unplanted probes.
3. The r3 gates ignored the real unplanted failure mode, a confident wrong answer (wifi -> studio code, OFF_FORMAT in
   every A and B block). r5 gates non-abstaining WRONG answers on unplanted probes, plus INVENTED.
4. The r4 prereg expectation flagged the unplanted-SKIP risk but did not pre-decide whether it was a regression. r5
   states the rule before scoring.
5. The r4 workflow ran only the r2 verdict in CI and gates were applied offline. r5 runs verdict and gates in-workflow,
   both freeze-checked.

## Comparison to r4 (report-only)
r4 receipts: run 45918447833: A 18/18, B 18/18, O 9/18, A/A 0, r2 KEEP, r3 gates REVERT (SKIP B 1 vs A 0 per block,
unplanted ticket workshop-3).

# C1-ctx-compress-r4 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict scripts frozen by sha256 in `ctx-compress-r4-FREEZE.sha256`
(checked in-workflow). Synthetic text only.

## Question
r3 made a reply-less user statement a fact, but facts were still tried newest first, and context_eval's noise pairs
(~189-char replies, under FACT_REPLY_CHARS) are facts too, so newer noise filled the 2810 budget before the oldest
reply-less turns (vendor, flight-old). Does trying facts by density (shortest exchange first) keep planted recall of
`compress_brief(brief, 2810)` at the full-brief level?

## The ONE change (production code)
`voice-adapter/brief_compress.py`: fact exchanges are tried shortest first (`sorted(..., key=exchange size)`, stable,
so equal sizes stay newest first); was newest first. Fact/filler test, filler order, budget, header/trailer rules as r3.
- Unit tests (`test_brief_compress_r4.py`, 5 tests; 3 fail on the frozen r3 copy, 5 pass on r4): an old reply-less
  fact is kept against newer long-reply facts at a budget where r3 drops it; equal sizes stay newest first; dry-run
  prediction below; recorded differential vs r3 (FULL, slm-v2-e5, slm-v2-live differ only at budget 500, OTHER
  byte-identical); budget and verbatim order on every fixture. `test_brief_compress_r3.py` now runs on the frozen r3
  copy (`bench/brief_compress_r3.py`, sha256 9f6ba9c2...).

## Harness: frozen r2 harness, unchanged
Same `bench/ctx_compress_r2_eval.py` (sha256 e5e0ef95...), `context_eval.py`, `lanespeed.py`, verdict
`bench/ctx_compress_r2_verdict.py` (sha256 c44d4ca0...), gates `bench/ctx_compress_r3_gates.py` (sha256 649eb88e...),
same 3 workshop scenarios x 8 probes (18 planted), same 6-job matrix (scenario x probe half), blocks A1 B1 A2 B2 O1
per job, fresh llama-server per block, greedy, seed 42, max 120 tokens, thinking off, cache_prompt false,
gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 ubuntu arm64 CPU, ubuntu-24.04-arm, deadline 2580 s.
A = full brief, B = production compress_brief (now r4), O = pre-r2 compressor (report-only).

## Primary metric and frozen rule (= r2 rule + r3 gates)
- Planted recall per block over the 18 planted probes; brief-region recall (studio/locker/vendor, of 9).
- r2 verdict, unchanged: KEEP iff min(B1, B2) >= max(A1, A2) - band, band = A/A flips (A1 vs A2); NULL on missing
  or incomplete jobs, missing primary block, request error, wrong scenarios, != 18 planted per block.
- Gates (r3 gates script, unchanged): for each of SKIP, SUPERSEDED, INVENTED, each B block count must be
  <= max(A1, A2) count; a failed gate turns KEEP into REVERT.
- Report-only: O1; first_sentence p50 pooled per arm (A, B, O) as secondary; B/B flips; check O1 < min(A).

## Expectation (I, from the offline dry-run, stated before scoring)
Dry-run (M, offline, dry-offline.json): at 2810 B now holds all six planted values in all 3 scenarios (studio, locker,
vendor, flight, room, parcel), B brief 2484-2485 chars (r3: 2797-2799, no vendor). B keeps all four reply-less turns
(vendor, flight-old, room, flight-new) plus 6 of the shortest noise pairs (not the newest). If recall follows context,
B reaches 18/18 and KEEP, unless the gates fail (r3's 3 SKIP were on unplanted probes such as ticket) or the kept
older flight-old line raises SUPERSEDED.

## Comparison to r3 (report-only)
r3 receipts: run 45912841415: A 18/18, B 15/18, O 9/18, brief-region 9/6/0, A/A 0, SKIP B 3 vs A 0,
p50 A 60.352 s, B 45.244 s.

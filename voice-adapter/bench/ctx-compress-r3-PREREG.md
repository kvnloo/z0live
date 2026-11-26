# C1-ctx-compress-r3 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, rule and verdict scripts frozen by sha256 in `ctx-compress-r3-FREEZE.sha256`
(checked in-workflow). Synthetic text only.

## Question
r2 lost the vendor fact because a user statement with no assistant reply was filed as filler. Does classing such a
statement as a fact keep planted recall of `compress_brief(brief, 2810)` at the full-brief level?

## The ONE change (production code)
`voice-adapter/brief_compress.py`: the fact test becomes `len(ex) == 1 or reply <= FACT_REPLY_CHARS`
(was `len(ex) > 1 and reply <= FACT_REPLY_CHARS`). Nothing else changes; budget, order, header/trailer rules as r2.
- Unit tests (`test_brief_compress_r3.py`, 6 tests): a reply-less user statement is kept at a budget where r2 drops
  it (fails on r2, passes on r3); byte-identical to the frozen r2 copy (`bench/brief_compress_r2.py`, sha256
  f5478ee3...) on FULL, OTHER, slm-v2-e5, slm-v2-live at 12 budgets; the ONLY changed fixtures are the three
  context_eval briefs (they hold reply-less user lines). `test_brief_compress_r2.py` now runs on the frozen r2 copy.

## Harness: frozen r2 harness, unchanged
Same `bench/ctx_compress_r2_eval.py` (sha256 e5e0ef95...), `context_eval.py`, `lanespeed.py`, verdict
`bench/ctx_compress_r2_verdict.py` (sha256 c44d4ca0...), same 3 workshop scenarios x 8 probes (18 planted), same
6-job matrix (scenario x probe half), blocks A1 B1 A2 B2 O1 per job, fresh llama-server per block, greedy, seed 42,
max 120 tokens, thinking off, cache_prompt false, gemma-4-E2B-it Q4_K_M (740185b2...), llama.cpp b12527 ubuntu arm64
CPU, ubuntu-24.04-arm, deadline 2580 s. A = full brief, B = production compress_brief (now r3), O = pre-r2
compressor (report-only).

## Primary metric and frozen rule (= r2 rule, plus the stated gates)
- Planted recall per block over the 18 planted probes; brief-region recall (studio/locker/vendor, of 9).
- r2 verdict, unchanged: KEEP iff min(B1, B2) >= max(A1, A2) - band, band = A/A flips (A1 vs A2); NULL on missing
  or incomplete jobs, missing primary block, request error, wrong scenarios, != 18 planted per block.
- Gates (`bench/ctx_compress_r3_gates.py`): for each of SKIP, SUPERSEDED, INVENTED, each B block count must be
  <= max(A1, A2) count; a failed gate turns KEEP into REVERT.
- Report-only: first_sentence p50 pooled per arm (A, B, O); B/B flips; discrimination check O1 < min(A).

## Expectation (I, from the offline dry-run, stated before scoring)
Dry-run (M, offline): at 2810 the reply-less turns now compete as facts newest first with context_eval's noise
pairs (~189-char replies, which are also facts). B keeps the two newest reply-less turns (room, flight-new) and the
stable header (studio, locker), and still drops the two oldest (vendor, flight-old); r2 kept flight-old as filler.
Vendor is absent from B's context in all 3 scenarios. If the model recalls only what is in context, B stays at 15/18
and the outcome is REVERT. A KEEP would need the model to miss vendor in A, or a band >= 3.

## Comparison to r2 (report-only)
r2 receipts: run 45907001327: A 18/18, B 15/18, O 9/18, brief-region 9/6/0, A/A 0, p50 A 60.124 s, B 44.215 s.

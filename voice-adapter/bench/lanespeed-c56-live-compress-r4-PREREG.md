# C56-live-compress-r4 prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU, Neoverse-N2 in every earlier C56 run). A CPU result here says nothing about phones, Metal or GPUs.

## Why this run
C56-live-compress-r3 (run 45875973926, PR #158): p50 27.585 -> 13.781 s, usable A 14/14 vs B 13/13 -> REVERT. The
must-skip case s-build ("Did my last build pass?") still answers a spoken non-answer in both B blocks, as in the even
run. r3 kept filler 6, 9, 12, 15 plus the newest exchange 17 (Japanese, always kept); the hand-built e5 brief (passed
s-build in C56-brief, run 45767553751) keeps filler 3, 6, 9, 12, 15 and not 17. Remaining difference: heat pump (3) in
e5 only, newest exchange (17) in live only. The newest exchange is the last text before the ask.

## The one change vs C56-live-compress-r3 (run 45875973926)
brief_compress.compress_brief no longer starts its priority order with the newest exchange. A fact newest exchange is
kept with the facts; a filler newest exchange is tried last, only if budget is left. Was: the newest exchange always
first. Budget 2810 unchanged. Header, Status line and all 6 fact exchanges still kept; filler spread rule unchanged.
New arm-B brief: 2810 chars (was 2672). Kept filler (0-based exchange index): 3 heat pump, 6 sci-fi books, 9 git
passphrase, 12 stretch, 15 sourdough; 17 Japanese dropped. = e5 set 5/5. The B brief text is byte-identical to the
slm-v2-e5.json brief (unit test asserts it), so arm B here replays the C56-brief even-case B arm brief through the live
compressor path (slm-v2-live.json, only note/derived fields differ from slm-v2-e5.json).
Everything else byte-identical: arm-A suite slm-v2.json, cases (--every 2 --offset 0, 15 even-index cases), model
gemma-4-E2B-it Q4_K_M 740185b2, llama.cpp b12527, server flags, sampling, scorer (slm_bench.py, tiers.py), lanespeed.py,
lanespeed_verdict.py (978c699e), both dry checks, timeouts. Workflow lanespeed-c56-live-compress-r4.yml is a copy of the
r3 workflow with only names, header comment, trigger branch/path, artifact names and SUITE_B_SHA changed. Trigger: the
push that adds it.

Tests: one new test (kept filler == [3, 6, 9, 12, 15], newest exchange not kept, output == e5 brief), red on the r3
compressor, green on r4. Two old tests updated only where they asserted keep-newest:
test_keeps_header_status_and_newest_turn (newest kept exchange is now the newest fact, adb) and
test_filler_spread_every_other (kept filler [6,9,12,15] -> [3,6,9,12,15]; assertIn(n-1) -> assertNotIn).

## Hypothesis
H_C56-live-r4: on the 15 even-index slm-v2 cases, B (compressed brief, newest exchange not force-kept) has a pooled
first-answer p50 lower than A (full brief) by more than the A/A spread, both B block p50s below both A block p50s,
usable(B_i) >= usable(A_i) for i = 1, 2 (paired, per block) and min(B) >= min(A) (frozen script).
Secondary (descriptive): s-build answered SKIP in B1 and B2.

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks (ABAB A1 B1 A2 B2, fresh
  llama-server per block). n = 15 cases per block.
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Gate: usable(B1) >= usable(A1) AND usable(B2) >= usable(A2) (paired), plus the frozen script's min(B) >= min(A).
  Usable = slm_bench.score: correct AND spoken.

## Decision rule (identical to C56-live-compress / -even / -r3)
- NULL: a block missing/incomplete/has a request error, or the run does not finish. One infra fix + one rerun, then NULL.
- REVERT: either gate fails.
- KEEP: p50(A) - p50(B) > A/A spread AND both B block p50s below both A block p50s AND both gates pass.
- NO_GAIN otherwise.

## Stop rule
One scored run. No reruns for outcome reasons. If s-build still fails, the next step is s-build x10 reps per brief
(A/A) to test whether the e5 pass was a near-tie; not run here.

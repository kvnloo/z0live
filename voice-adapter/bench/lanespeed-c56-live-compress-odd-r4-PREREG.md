# C56-live-compress-odd-r4 prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU, Neoverse-N2 in every earlier C56 run). A CPU result here says nothing about phones, Metal or GPUs.

## Why this run (replication, stated up front)
C56-live-compress-r4 (run 45882878587, PR #161, branch ci/lanespeed-c56-live-compress-r4, commit 8bf059d) was KEEP on
the 15 even-index slm-v2 cases: p50 A 27.622 s -> B 13.929 s, A/A 0.092 s, usable 14/14/14/14. The r4 compressor
output (arm B brief, 2810 chars) is byte-identical to the hand-built slm-v2-e5.json brief. The e5 brief was already
scored on the odd half in C56-brief-odd (run 45773497094): KEEP, A 27.823 s, B 14.245 s, usable 13/13/13/13.
The earlier odd-half live-compressor run C56-live-compress (run 45850240823, PR #154, --offset 1) used the OLD
keep-newest compressor (2777-char brief, different filler) and was KEEP, usable 13/13/13/13.

So this run is partly a replication. Its purpose: confirm the shipped r4 compressor code path (slm-v2-live.json,
checked in CI by `lane_brief_live.py --check` and the compressor unit tests) on the odd half. It is NOT new information
about the e5 brief text; any B-arm result is expected to replicate C56-brief-odd B (14.245 s, 13/13) within noise.

## The one change vs C56-live-compress-r4 (run 45882878587)
Case selection `--every 2 --offset 1` (the 15 odd-index cases) instead of `--offset 0`. Workflow
lanespeed-c56-live-compress-odd-r4.yml is a copy of lanespeed-c56-live-compress-r4.yml with only names, header
comment, trigger branch/path, concurrency group, output/artifact names and `--offset 1` changed. Trigger: the push
that adds it. Everything else byte-identical: compressor (brief_compress.py r4), arm-A suite slm-v2.json, arm-B suite
slm-v2-live.json (SUITE_B_SHA 877737e6, = committed live brief), budget 2810, model gemma-4-E2B-it Q4_K_M 740185b2,
llama.cpp b12527, server flags, sampling, scorer (slm_bench.py, tiers.py), lanespeed.py, lanespeed_verdict.py
(978c699e), both dry checks, unit tests, timeouts, runner ubuntu-24.04-arm, block order ABAB A1 B1 A2 B2.

## Hypothesis
H_C56-live-odd-r4: on the 15 odd-index slm-v2 cases, B (r4 compress_brief brief) has a pooled first-answer p50 lower
than A (full brief) by more than the A/A spread, both B block p50s below both A block p50s, usable(B_i) >= usable(A_i)
for i = 1, 2 (paired, per block) and min(B) >= min(A) (frozen script).

## Metrics
- Primary: first_sentence_s. Arm p50 = median of the 30 pooled values of its two blocks (fresh llama-server per
  block). n = 15 cases per block.
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Gate: usable(B1) >= usable(A1) AND usable(B2) >= usable(A2) (paired), plus the frozen script's min(B) >= min(A).
  Usable = slm_bench.score: correct AND spoken.
- Descriptive: B vs C56-brief-odd B (14.245 s, 13/13); which cases are unusable (C56-brief-odd: f-everest, s-mom).

## Decision rule (frozen; identical to C56-live-compress / -even / -r3 / -r4)
- NULL: a block missing/incomplete/has a request error, or the run does not finish. One infra fix + one rerun, then NULL.
- REVERT: either gate fails.
- KEEP: p50(A) - p50(B) > A/A spread AND both B block p50s below both A block p50s AND both gates pass.
- NO_GAIN otherwise.

## Stop rule
One scored run. No reruns for outcome reasons. The task is bounded to 50 minutes wall; a run not finished by then is NULL.

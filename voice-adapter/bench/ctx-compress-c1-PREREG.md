# C1-ctx-compress prereg (written before any scored run; never edited after scoring)

Requirement: C1/C2 context (lanes must keep the user's facts). Track E. Device: GitHub Actions `ubuntu-24.04-arm` CPU
runner, same pool, model and llama.cpp build as C56-live-compress-r4. A CPU result says nothing about phones or GPUs.

## Why this run
C56-live-compress-r4 (branch ci/lanespeed-c56-live-compress-r4, PR #161 / #162) kept brief_compress.compress_brief:
first-answer p50 27.622 -> 13.929 s (even) and 27.796 -> 14.249 s (odd), usable gate equal on slm-v2. Usable on slm-v2
is not a context-recall test. This run asks whether the same compressor keeps planted-fact recall.

## Suite (what is scored)
context_eval.py (Track E) planted-fact suite, cases and scorer unchanged: 3 synthetic "workshop" scenarios x 8 probes
(6 planted: studio, locker, vendor, flight, room, parcel; 2 absent: ticket, wifi). The planted facts are NOT in slm-v2;
context_eval has its own synthetic suite (stable brief + 2 chat facts early in a long noise chat + corrections and a
parcel code in the call turns). Scorer = context_eval.score (RECALL / SUPERSEDED / INVENTED / SKIP / OFF_FORMAT / ERROR);
recall = RECALL count over planted probes (context_eval.summarize's definition). No private data, no model grader.

## The one change (A vs B)
- A: context = context_eval.snapshot(scenario) = tiers.snapshot(stable + chat brief, call turns) (unchanged).
- B: context = tiers.snapshot(brief_compress.compress_brief(same brief, 2810), call turns).
System prompt = tiers.SYSTEM.format(tier="fast") + "\n\n" + context in both arms; ask = the probe text. Everything else
byte-identical in both arms: model gemma-4-E2B-it Q4_K_M 740185b2, llama.cpp b12527, lanespeed.serve flags (-c 8192
-np 1 --cache-ram 0 --jinja -ub 512), lanespeed.ask (stream, temperature 0, seed 42, max_tokens 120, thinking off,
cache_prompt false). Driver: bench/ctx_compress_eval.py (new, unit-tested: arm A == context_eval.snapshot; B with an
unbounded budget == A; only the brief differs). Transport is lanespeed.ask rather than eval_local.LocalModel so latency
is comparable with C56; context_eval's scorer is applied to the visible reply unchanged.

## Mechanism note (M, computed offline before the run, `ctx_compress_eval.py --dry`)
The context_eval brief labels turns "user:"/"assistant:" (lowercase), so compress_brief finds no speaker structure and
falls back to the newest 2810 chars. tiers.snapshot already keeps only the newest 6000 brief chars in A. In both arms
the stable brief and both chat facts (studio, locker, vendor) are outside the kept brief; the planted values present
in context are flight, room, parcel (call turns, untouched) in both arms, all 3 scenarios. So max recall is 9/18 per
block in both arms by construction; B removes ~3190 chars of noise only. System chars A/B: 10765/7575, 12435/9245,
14111/10921. This run therefore tests whether compression changes recall/supersession behaviour given the same facts,
not whether compress_brief preserves facts that sit in the dropped brief region (it would not: its fallback is a tail
cut). Reported as a limitation, not a reason to change the design after seeing results.

## Design
Matrix of 3 jobs (one scenario each, one runner each, 45 min timeout). Each job: ABAB fresh llama-server blocks
A1 B1 A2 B2, 1 unscored warm-up + 8 probes per block, deadline 2160 s. Per-block numbers summed over the 3 jobs
(18 planted probes per block label).

## Metrics
- Primary: recall count per block (A1, B1, A2, B2), higher better.
- Noise band: A/A flips = planted probes (scenario, key) whose RECALL status differs between A1 and A2, summed over
  jobs. band = A/A flips. B/B flips (B1 vs B2) reported only (a noisy B arm must not widen its own band).
- Secondary (descriptive): SUPERSEDED / INVENTED / absent SKIP counts per block; first_sentence_s p50 per arm (pooled).

## Decision rule (frozen script bench/ctx_compress_verdict.py)
- NULL: a job missing or incomplete, a block missing, any request ERROR, or jobs not covering workshop-1..3. One infra
  fix + one rerun, then NULL.
- KEEP: min(recall B1, B2) >= max(recall A1, A2) - band (so each B block is no lower than every A block by more than
  the A/A band).
- REVERT otherwise.

## Stop rule
One scored run. No reruns for outcome reasons. If KEEP, the open question is facts in the dropped brief region: a
follow-up would render the context_eval brief with capitalised speaker labels (compress_brief's fact path) as its own
preregistered change.

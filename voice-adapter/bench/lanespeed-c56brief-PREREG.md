# C56-brief prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU; Neoverse-N2 in every earlier arm run, including C56). A CPU result here verdicts nothing about phones, Metal
or GPUs.

## Hypothesis
H_C56b: on gemma4-e2b Q4_K_M with CPU llama.cpp b12527, replacing the full slm-v2 lane brief (5540 chars) with the
shorter e5 brief (2810 chars) lowers the cold (no prompt cache) first-answer-sentence p50 by more than the A/A spread,
without lowering the usable count on the same cases.

## Why this change, and why e5
- C56 (run 45761217254) measured that cold first-answer latency on this runner is almost all prefill: about 27.6 s for
  about 1489 prompt tokens (~55 tok/s). The brief is most of the prompt, so a shorter brief is the lever with the
  largest expected payoff. Expected B prompt about 840 tokens (inferred: the fake-server dry check gives a chars/4
  ratio of 885/1568 = 0.56 for B/A), so B first sentence about 15-16 s (inferred) if prefill stays linear.
- Brief-quality evidence (ledger GH9-GH15, all Q4_K_M lane-bar runs on the same runner class): the briefs that passed
  the Q4 lane bar were half (even filler pairs, 3269 chars, GH9 Q4 27/30), es4 (3289, GH12 27/30), es2 (GH13 27/30),
  es24 (3269, GH15 27/30) and e5 (2808, GH11 Q4 27/30, inv 0, skip 7/8, zero Q4 item differences vs half). es3 (GH14)
  lost u-wait at Q4 (26/30). The rule from the task is: the shortest brief that passed the Q4 bar. That is e5 (2808
  chars, about 14 % shorter than half/es24). e5 failed the Q8 bar by one item (r-apples) and so was never kept for Q8;
  this climb is Q4_K_M only, so that failure does not apply here, and the usable gate below still guards quality.
- The GH-series suites were built from the pre-privacy-scrub slm-v2 text (other speaker label and other name for one
  fact). C56 froze the public scrubbed slm-v2.json (sha256 7fa9f7c6). To keep exactly one change, the B suite is built
  from that frozen file with the same GH11 e5 rule (`lane_brief_e5.py`; the same rule with the pre-scrub label and name
  was checked offline to reproduce the GH11 e5 brief byte-for-byte from the pre-scrub text). Cases, ask text and every other field are byte-identical; only the brief
  differs (2810 chars vs GH11's 2808; the difference is the scrubbed names). The Q4 quality evidence for e5 is
  therefore inferred for the scrubbed text, not measured; the gate measures it.

## What earlier preregs got wrong, and what this one changes
1. C56 run 1 assumed 30 cases x 4 blocks fit in the 45 min cap. Cold prefill is about 27 s per request, so it did not
   fit (NULL; AMEND-1 cut to 15 even-index cases). Change: the case count is decided here, before any run, from the
   measured C56 timings. All 30 cases: A 2 x 31 x 28 s = 1736 s plus B 2 x 31 x ~16 s = 992 s, about 2730 s. That is
   more than the 1980 s in-script deadline, so it does not fit. 15 even-index cases (as C56 run 2): A 2 x 16 x 28 =
   896 s plus B 2 x 16 x ~16 = 512 s plus loads, about 1450 s (about 24 min). That leaves about 530 s of margin under
   the 1980 s deadline. Decision: 15 even-index cases per block (`--every 2`), deadline 1980 s, job cap 45 min.
2. GH1-GH4 mixed CPUs across separate runs; GH2 compared against another run. Kept from C56: one job, one arm runner,
   blocks A1 B1 A2 B2, A/A control in the same job, `lscpu` saved.
3. slm_bench `first_s` hides prefill. Kept from C56: metric = first complete answer sentence with the cache off.
4. C56 B changed prefill split and flipped a near-tie greedy text (u-wait). Here a brief change will change greedy text
   a lot more, so text equality A-vs-B is descriptive only; the usable gate is the quality check.
5. C56's verdict script reports prompt_n for A1 only. The verdict script is reused unchanged (frozen); B prompt_n is
   read from the receipt block summaries and reported next to it.

## Frozen inputs (sha256 in FREEZE)
- Model: unsloth/gemma-4-E2B-it-GGUF rev 0314792d7f1f7e229411f620751375812bb9faf2, gemma-4-E2B-it-Q4_K_M.gguf,
  sha256 740185b21d22ceb83a11c3aa62ad5842ef32c70f6096d756bbee85a1e4ec34b8 (same file as C56, GH1-GH16; checked on the
  runner).
- Arm A suite: voice-adapter/bench/slm-v2.json sha256 7fa9f7c64ce0a68741232a46707edecf2dd74e2403232e0c211aad03a792367c.
- Arm B suite: voice-adapter/bench/slm-v2-e5.json sha256 add175760279a64dece3f4c1df895f62192965882d386a56c64fce69dd889887
  (built by lane_brief_e5.py; same 30 cases).
- llama.cpp b12527 ubuntu-arm64 prebuilt, tarball sha256 c2758c87 (same as C56).
- Lane prompt and scorer: voice-adapter/slm_bench.py 805a2518, voice-adapter/tiers.py 8496ab2c (unchanged).
- Harness: lanespeed.py (adds `--suite-b` only), lanespeed_verdict.py (byte-identical to C56), workflow
  lanespeed-c56brief.yml, dry check lanespeed_c56brief_check.py.

## Procedure
One job. Blocks A1 B1 A2 B2. Each block starts a fresh llama-server with identical flags in both arms:
`-c 8192 -np 1 --cache-ram 0 --jinja --no-webui -ub 512` (the default; `-b` 2048 default; threads default). One
unscored warm-up (case 0 with the arm's brief), then the 15 even-index cases in suite order, once each, streamed,
`temperature 0, seed 42, max_tokens 120, enable_thinking false, cache_prompt false`, messages by `slm_bench.messages`
with the arm's suite, scored by `slm_bench.score`.

## Metrics
- Primary: first_sentence_s (request to first sentence end in the visible stream; no terminator = end of stream).
  Arm p50 = median of the 30 pooled values of its two blocks.
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Quality gate: usable count of each B block >= the lower of the two A blocks (same 15 cases).
- Descriptive: TTFT p50, prompt_ms p50, prompt_n per arm, paired per-case difference, text equality, load time.

## Decision rule (lanespeed_verdict.py, unchanged)
- NULL if a block is missing, incomplete or has a request error (one infra fix + one rerun allowed, then NULL).
- REVERT if the quality gate fails.
- KEEP if p50(A) - p50(B) > A/A spread AND both B block p50s are below both A block p50s AND the gate passes.
- NO_GAIN otherwise.
A KEEP applies only to this runner CPU, model file, build and the synthetic slm-v2 suite. It is a candidate for the
lane brief budget, never a verdict for phones or for the Q8 / larger-model lanes (e5 failed the Q8 bar in GH11).

## Power note
n = 15 cases x 2 blocks per arm. The expected effect (about -40 % prefill) is far larger than C56's A/A spread
(0.057 s); the gate is the binding risk. With 15 cases, one usable flip decides the gate.

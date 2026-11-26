# C56-lanespeed prereg (written before any scored run; never edited after scoring)

Requirement: C5/C6 lanes (fast lanes must be fast). Track D. Device: GitHub Actions `ubuntu-24.04-arm` CPU runner
(4 vCPU; Neoverse-N2 in every earlier arm run). A CPU result here verdicts nothing about phones, Metal or GPUs.

## Hypothesis
H_C56: on the lane-class model gemma4-e2b Q4_K_M with CPU llama.cpp b12527, raising the llama-server physical batch
size from the default `-ub 512` to `-ub 2048` lowers the cold (no prompt cache) lane first-answer latency p50 on the
frozen public slm-v2 suite by more than the A/A spread, without lowering the usable count.

## Why this knob (expected info gain per cost)
- Every slm-v2 lane request carries the ~5.5k-char brief (about 1.4k prompt tokens). With no prompt reuse, time to the
  first answer sentence is mostly prefill; decode to the first sentence is about 10 tokens. With `-ub 512` the prompt
  runs as 3 ubatches; with `-ub 2048` it runs as one. Larger CPU GEMM tiles may raise prefill throughput, or may not
  (cache pressure). The sign is uncertain, so the information gain is high. The cost is one job and no extra download.
- Speculative decoding (draft vs none) speeds decode only, not prefill, and needs a vocab-compatible gemma4 draft GGUF
  that no earlier z0live run pinned: more download and infra risk for a smaller share of the metric. Not chosen.
- Thread count: llama.cpp already defaults to all 4 runner cores; little headroom. Not chosen.
- Prompt-prefix reuse: already tested on the phone (S25-B3q, S25-B3r). Excluded; both arms send `cache_prompt: false`
  and start the server with `--cache-ram 0`.

## What earlier preregs got wrong, and what this one changes
1. GH1-GH4 compared separate runs on the x64 pool, which mixes Intel and AMD CPUs; A/A flips came from CPU changes.
   Change: one job on one arm runner, arms in alternating blocks A1 B1 A2 B2; `lscpu` saved with the result.
2. GH2 compared its number with a baseline from a different run ("inferred"). Change: baseline and candidate are in
   the same job; the A/A control (A1 vs A2) is in the same job.
3. GH1/GH2 lost a cell to a stalled 5 GB HF download at a 50 min timeout. Change: smaller model (3.1 GB), pinned HF
   revision, actions cache, curl speed floor, 10 min download step, 45 min job, and a 33 min in-script deadline that
   writes a partial receipt; an incomplete receipt is verdict NULL.
4. slm_bench `first_s` is the first visible token with the brief already in the prompt cache, so it hides prefill.
   Change: the metric is the first complete answer sentence with the cache off (defined below); TTFT is secondary.
5. S25-B3q showed that a different prefill split flips greedy text by fp16 near-ties. Change: greedy text equality is
   reported but does not gate; the quality gate is the slm_bench usable count, and the A/A text equality is reported
   so a B-vs-A text change can be read against it.
6. D-truth-* used speed only as a descriptive wall time. Change: latency is the primary metric with a frozen rule.

## Frozen inputs (sha256; see FREEZE in the evidence dir)
- Model: unsloth/gemma-4-E2B-it-GGUF rev 0314792d7f1f7e229411f620751375812bb9faf2, gemma-4-E2B-it-Q4_K_M.gguf,
  3106738272 bytes, sha256 740185b21d22ceb83a11c3aa62ad5842ef32c70f6096d756bbee85a1e4ec34b8 = HF LFS oid at that rev
  (checked before the run); the same file as GH1-GH16. Format: GGUF Q4_K_M (not the vendor bf16).
- Suite: voice-adapter/bench/slm-v2.json sha256 7fa9f7c64ce0a68741232a46707edecf2dd74e2403232e0c211aad03a792367c
  (30 cases: 6 fact, 6 reasoning, 6 context, 4 followup, 8 must-SKIP).
- Lane prompt and scorer: voice-adapter/slm_bench.py 805a2518 (same as GH1-GH16), voice-adapter/tiers.py 8496ab2c.
- llama.cpp b12527 ubuntu-arm64 prebuilt, tarball sha256 c2758c87 (same as GH5+, D-truth-*).
- Harness: voice-adapter/bench/lanespeed.py, lanespeed_verdict.py (sha256 in the freeze file).

## Procedure
One job. Four blocks in the order A1 B1 A2 B2. Each block starts a fresh llama-server:
`-c 8192 -np 1 --cache-ram 0 --jinja --no-webui -ub N` (A: N=512, the default; B: N=2048; `-b` stays at its 2048
default; threads default). One unscored warm-up request, then the 30 cases in suite order, once each, streamed,
`temperature 0, seed 42, max_tokens 120, enable_thinking false, cache_prompt false`, messages built by
`slm_bench.messages` and scored by `slm_bench.score` (unchanged).

## Metrics
- Primary: first_sentence_s = wall time from sending the request until the streamed visible reply first contains a
  sentence end (`.`, `!` or `?` followed by whitespace); a reply with no such end (for example `SKIP`) counts at the
  end of the stream. Arm p50 = median of the 60 pooled values of its two blocks.
- Noise band: A/A spread = |p50(A1) - p50(A2)|. B/B spread reported.
- Quality gate: usable count (slm_bench) of each B block >= the lower of the two A blocks.
- Secondary (descriptive): TTFT p50, server prompt_ms p50, total p50, paired per-case difference, greedy text
  equality A1-A2, B1-B2, A1-B1, A2-B2, load time, lscpu.

## Decision rule (lanespeed_verdict.py)
- NULL if a block is missing, incomplete or has a request error (one infra fix + one rerun allowed, then NULL).
- REVERT if the quality gate fails.
- KEEP if p50(A) - p50(B) > A/A spread AND both B block p50s are below both A block p50s AND the quality gate passes.
- NO_GAIN otherwise (including B slower).
A KEEP applies only to this runner CPU, this model file and llama.cpp build; it is a candidate for the phone and
Studio lanes, never a verdict for them.

## Power note
n = 30 cases x 2 blocks per arm. This is a single-runner smoke-scale climb: it can detect a prefill change of more than
a few percent if block-to-block noise is small; it cannot detect small effects. The A/A spread is measured, not assumed.

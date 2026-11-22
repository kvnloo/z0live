# GitHub CPU text climb (branch ci/text-climb)

`.github/workflows/text-climb.yml` scores small GGUF lane models (Qwen3-0.6B, Qwen3-1.7B, Gemma-4-E2B-it; Q4_K_M and
Q8_0, unsloth GGUFs pinned by sha256) on the frozen lane suites with `voice-adapter/slm_bench.py` unchanged, on
llama.cpp b12527 prebuilt ubuntu-x64 CPU. Brief variants: full (slm-v1, slm-v2) and compressed
(`suites/slm-v2-model1100.json`, the frozen B12 qwen3.5-9b fact list from `bench/lane_compress.py`). Synthetic
suites only. `ci_bench.py` adds the Track B gate fields (must-SKIP of 8, invented-fact heuristic); `summarize.py`
writes the job-summary table. Runner TTFT is GitHub CPU, not phone or Studio. Runs on push to this branch only.
The same harness lives in the private fleet repo kvnloo/z0-hillclimb (branch z0live/text-climb).

# On-device lane models: phone speed, 2026-11-19

llama.cpp b12527 `llama-bench`, lane-shaped: `-p 1024` (lane prompt + call brief is about 1k tokens) and `-n 64`
(one spoken answer), 3 repetitions, 4 threads. Time to first word is estimated as 1024 / prompt speed.
Script: `voice-adapter/bench/phone_bench.sh` (runs on the desktop host over adb). Receipts on the desktop host:
`/mnt/zer0models/workspace/zer0/data/z0live/phone-bench-20261119-{2137,2155,2223}`.

| Device | Model | Backend | Prompt tok/s | Reply tok/s | First word (est.) |
|---|---|---|---|---|---|
| S25 Ultra (SM-S938U) | Gemma 4 E2B | GPU (Adreno 830) | 1082.5 | 25.0 | **0.95 s** |
| S25 Ultra | Qwen3-1.7B | NPU (Hexagon v79) | 1097.7 | 39.1 | **0.93 s** |
| S25 Ultra | Qwen3.5-2B | NPU | 975.6 | 33.2 | 1.05 s |
| S25 Ultra | Qwen3-1.7B | GPU | 473.3 | 35.2 | 2.16 s |
| S25 Ultra | LFM3-1.7B | GPU | 364.1 | 33.9 | 2.81 s |
| Tab (TB350FU) | LFM3-1.7B | CPU (generic build) | 38.4 | 10.3 | 26.7 s |
| Tab | Qwen3-1.7B | CPU | 34.0 | 9.5 | 30.1 s |
| Tab | Qwen3.5-2B | CPU | 32.5 | 7.9 | 31.5 s |

Not measured, and why:
- **Gemma 4 E4B on the S25:** 4.6 GB model, 4.6 GB free on the phone.
- **Gemma 4 E2B and LFM3 on the S25 NPU:** the experimental Hexagon backend segfaults on both (they run on the GPU).
- **S22 (SM-S906U1), every model:** its v69 NPU has no htp library in b12527 and the Hexagon registry still starts
  with `-dev GPUOpenCL`; the GPU run ended without a result and the phone then dropped off wireless adb.

What it means (inferred): the S25 can host a fast lane at about 1 s to first word, on par with the hosted fast
lanes (Haiku 5.5 warm about 0.87 s on the Studio), with Gemma 4 E2B on the GPU or Qwen3 on the NPU. Gemma 4 E2B
scored 25/30 on slm-v1 (Qwen3-1.7B 22/30), so it is the first on-phone candidate. The tablet is too slow for a
call lane.

Update 11-20 (Track B, docs/HILLCLIMB-OVERNIGHT.md B10-B15): with a ~300-token brief held in the prompt cache and
prefilled at call start, the tab's Qwen3-1.7B CPU lane answers in ~1.1 s warm. Its Mali GPU runs once patched
(`voice-adapter/bench/ggml-vulkan-mali-vk11.patch`) but is 9.6x slower than its CPU at prefill.

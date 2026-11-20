# Fast text-lane pick, 2026-11-20

Pick **Gemma 4 E4B Q4_0 on Studio** and **Gemma 4 E2B Q4_0 on S25 GPU**. For offline fallback, use **Qwen3-1.7B Q4_K_M on tablet** and **Gemma 4 E2B Q4_0 on MBP**. Keep the live fast lane off both devices. These are inferred recommendations, not a deployment. The quality gate is **at least 22 usable out of 30 and zero harmful**.

## Results

Size is measured GGUF file size in decimal GB. Quant is read from the GGUF and, for Liquid QAD, checked against the published file. Quality columns are measured on Studio. `fresh` means a local run for this report; `cache` means the later matching slm-v1 receipt. `Must-SKIP` uses the suite's uppercase-token rule, not an exact-string match. Harmful is a manual count of must-SKIP cases with fabricated facts or claimed access/action, even if `SKIP` would suppress the reply. Refusals and malformed text alone are not harmful under this definition.

Device cells are **inferred TTFT@300 in seconds / inferred TTFT@1024 in seconds / measured decode tok/s**. TTFT is `prompt tokens / prompt tok/s`. It excludes model load, HTTP, and the delay before text is delivered; it is not a streaming TTFT measurement. Decode is the separate `tg64` test, not decode after the stated prompt. `n/a` means no comparable result. No numbers are guessed.

| Model | Size (GB) | Quant | slm-v1 usable/30 | Must-SKIP /8 | Harmful | Studio Metal | Tablet CPU | MBP CPU | S25 |
|---|---:|---|---:|---:|---:|---|---|---|---|
| gemma4-e2b | 2.84 | Q4_0 | 25/30 fresh | 6/8 | 0 | 0.0242 / 0.0575 / 149.9 | n/a | 4.7098 / 15.5670 / 7.2 | n/a / 0.95 / 25.0 GPU |
| gemma4-e4b | 4.59 | Q4_0 | 28/30 fresh | 8/8 | 0 | 0.0645 / 0.1731 / 94.3 | n/a | n/a | n/a |
| granite4.2-3b | 2.24 | Q4_K_M | 20/30 cache | 2/8 | 1 | 0.0795 / 0.2288 / 141.1 | n/a | n/a | n/a |
| k2-horizon-3.7b | 3.16 | Q4_K_M | 27/30 cache | 7/8 | 0 | 0.0944 / 0.2723 / 122.2 | n/a | n/a | n/a |
| lfm2-700m-q4km | 0.47 | Q4_K_M | 11/30 fresh | 3/8 | 2 | 0.0194 / 0.0519 / 417.7 | n/a | n/a | n/a |
| lfm2.5-1.2b-q4km | 0.73 | Q4_K_M | 18/30 fresh | 6/8 | 0 | 0.0268 / 0.0745 / 401.4 | n/a | n/a | n/a |
| lfm2.5-1.2b-qad-q4_0 | 0.70 | Q4_0 QAD | 21/30 fresh | 5/8 | 0 | 0.0252 / 0.0694 / 375.5 | n/a | n/a | n/a |
| lfm2.5-350m-qad-q4_0 | 0.22 | Q4_0 QAD | 13/30 fresh | 7/8 | 2 | 0.0100 / 0.0258 / 615.5 | n/a | n/a | n/a |
| lfm3-1.7b | 1.10 | Q4_K_M | 19/30 fresh | 8/8 | 0 | 0.0395 / 0.1080 / 224.3 | n/a / 26.67 / 10.3 | n/a | n/a / 2.81 / 33.9 GPU |
| minicpm5-2b | 1.56 | Q4_K_M | 22/30 cache | 8/8 | 0 | 0.0549 / 0.1502 / 181.3 | n/a | n/a | n/a |
| nanbeige4.2-3b | 2.54 | Q4_0 | 23/30 cache | 8/8 | 0 | 0.1587 / 0.4306 / 85.5 | n/a | n/a | n/a |
| qwen3-0.6b-q4km | 0.40 | Q4_K_M | 18/30 fresh | 3/8 | 3 | 0.0183 / 0.0495 / 427.8 | n/a | 3.4165 / 13.3223 / 25.3 | n/a |
| qwen3-0.6b | 0.64 | Q8_0 | 18/30 cache | 4/8 | 3 | 0.0176 / 0.0477 / 360.2 | n/a | n/a | n/a |
| qwen3-1.7b | 1.11 | Q4_K_M | 22/30 fresh | 8/8 | 0 | 0.0393 / 0.1065 / 223.2 | n/a / 30.13 / 9.5 | 8.3143 / 35.3505 / 9.3 | n/a / 0.93 / 39.1 NPU; n/a / 2.16 / 35.2 GPU |
| qwen3.5-2b | 1.28 | Q4_K_M | 22/30 cache | 4/8 | 1 | 0.0435 / 0.1221 / 196.6 | n/a / 31.51 / 7.9 | n/a | n/a / 1.05 / 33.2 NPU |
| qwen3.5-4b | 2.74 | Q4_K_M | 25/30 cache | 6/8 | 3 | 0.1024 / 0.3021 / 108.0 | n/a | n/a | n/a |

## Pareto front and picks

The fronts are inferred from candidates with device results that pass the quality gate. Lower TTFT at each available prompt length, higher decode speed, and higher raw usable count are the objectives. Size and SKIP are decision factors, not Pareto axes. An absent speed result cannot dominate or be dominated.

- **Studio front:** Gemma 4 E4B, Gemma 4 E2B, Qwen3-1.7B, and K2 Horizon on the raw suite scores. K2 leaks a thinking delimiter on five replies the suite counted as usable (measured); do not ship it as-is. The other three form the practical front. Pick **E4B** for the best measured quality and clean SKIP behavior. Pick E2B if prompt latency matters more and two spoken refusals in the suite are acceptable. In the local streaming quality run, E4B took 36 ms median to first visible text and 115 ms on the uncached first case; E2B took 18 ms and 41 ms (measured). Those are local HTTP times, not voice turn times.
- **Tablet front:** Qwen3-1.7B is the only measured model that clears the gate. Keep **Qwen3-1.7B Q4_K_M with six CPU threads and prefix caching** as a fallback. Keep the live fast lane off-device. The overnight B6/B10 separate streaming tests measured 2.39 s warm and 30.9 s to load the full brief, or 1.10 s warm and 7.5 s for the shorter, 304-token brief. That is not a 300-token `llama-bench` result. Do not ship a shortened brief on speed alone: B12's separate context tests measured recall loss for a different truncation.
- **MBP front:** Gemma 4 E2B and Qwen3-1.7B among the measured models. Pick **E2B** for offline fallback. It has higher measured quality and lower inferred prefill latency at both prompt lengths. Qwen decodes faster (measured), but neither qualifies as a live fast lane at the observed speeds. The smaller Qwen misses the quality gate.
- **S25 front:** Gemma 4 E2B on GPU and Qwen3-1.7B on NPU. Pick **E2B GPU** for higher Studio quality and similar cold prefill estimates in the archive. The prefill difference is smaller than the historical variation; Qwen on NPU decodes faster (measured). The overnight B7 Qwen GPU prefix-cache run measured 0.18 s warm median [0.166, 0.181] versus 2.92 s without cache on a different workload. It supports testing Qwen with a warm brief later, not inferring warm E2B or NPU TTFT. The S25 was not contacted for this report.

## Evidence and limits

- **Quality:** `voice-adapter/slm_bench.py --models <name>` ran the unchanged `voice-adapter/bench/slm-v1.json` with the branch's v2 lane prompt locally. No hosted model or judge ran. Quality receipts contain the frozen fixture; only aggregate results are in this report. The cached rows are from `~/.local/share/z0live/bench/slm-v1-20261119-1348.json`, matched by model slug and recorded file size against the local quant. That receipt does not include model hashes. The earlier `1343` receipt used the earlier prompt and is not pooled. The fresh Qwen3-1.7B, both Gemmas, and LFM3 reproduced the later receipt's aggregate counts. All rows had complete case coverage and no request errors (measured); that does not imply every answer was well formed. LFM3 emitted broken template text and fell below the gate on the v2 prompt.
- **Studio:** `llama-bench` b12527 (`78e05298f`), `-p 300,1024 -n 64 -ngl 99 -t 4 -r 5 -o json`. Entries use the mean rates. Fresh speed and quality receipts are in `~/.local/share/z0live/bench/textpick-20261120/`. Five additional interleaved Studio passes (`check-*.json`) preserved the latency and decode order of both Gemmas, Qwen3-1.7B, and MiniCPM5. For E4B the inferred median [range] at 1024 was 0.174 s [0.173, 0.199]; decode measured 97.1 [81.6, 97.2] tok/s. For E2B it was 0.058 s [0.058, 0.069]; decode measured 158.1 [131.3, 159.3] tok/s. Prefill and decode rank differently; the benchmark does not isolate a hardware bottleneck. No production voice services were restarted.
- **MBP:** b12527 Haswell CPU backend, `-p 300,1024 -n 64 -ngl 0 -t 4 --poll 0 -r 3 -o json`. The labeling sweep stayed running. Early attempts were discarded when the host changed priority after launch. The retained run verified absolute nice 10 across all benchmark threads throughout the timed phases. `ps` showed the four benchmark threads busy during the runs; CPU and memory-bandwidth limits were not isolated. At 1024, inferred single-sample prefill times ranged from 14.24 to 16.61 s for E2B and 33.68 to 37.83 s for Qwen3-1.7B. These are sequential measurements under background load, not idle-host or tuned maxima. SHA-256 matched Studio for all three tested GGUFs. Receipts are in `~/models/textpick-20261120/` on MBP and in the Studio receipt directory.
- **Android:** [Phone speed archive](PHONE-BENCH-20261119.md) and `~/.local/share/z0live/overnight.log` (B6, B7, B9, B10, B12). Existing `llama-bench` receipts on groot use b12527, four threads, and three repetitions. The archived Android GGUF source files match the corresponding Studio files by SHA-256. The reported decode rates are historical device measurements; quality transfers across backends are inferred, not guaranteed. There is no comparable Android `pp300` result in these sources.
- **Liquid:** [LFM2.5-350M-GGUF](https://huggingface.co/LiquidAI/LFM2.5-350M-GGUF) and [LFM2.5-1.2B-Instruct-GGUF](https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF) are official. No LFM2.5-700M text GGUF turned up in the Hub search. The older official [LFM2-700M-GGUF](https://huggingface.co/LiquidAI/LFM2-700M-GGUF) is marked as LFM2 in the table. All downloaded files match the Hub LFS hashes. The best Liquid quality was 21/30 (measured); none qualifies.

Before switching a live lane, verify the chosen backend on-device with a synthetic multi-turn call. Check first spoken answer, barge-in, and non-duplicate follow-ups. This report ranks local text models, not end-to-end voice behavior.

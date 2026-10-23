# Isolated local voice Pareto benchmark

The first sweep for issue #44 follows [Z0L1-BENCH](https://github.com/kvnloo/z0live/issues/44#issuecomment-6356978861). It reuses the six exact synthetic utterances from `desktop/tests/make_test_speech.sh` and adds explicit correction and cancellation utterances. It never opens a microphone, speaker, phone, live broker, or native task. Audio and per-trial output belong in a private scratch directory, not Git.

## Run

Install optional model dependencies into an isolated environment (`torch`, `transformers`, `faster-whisper`, `moshi`, `kokoro`, `pocket-tts`, `psutil`, `soundfile`, and Piper). Pin the environment and checkpoint revisions in the retained run receipt. Cached Qwen3-0.6B is a bounded reflex baseline, not a substitute for the owner's selected brain. No tools are supplied to it; a local answer cannot be called a completed computer action.

```sh
python desktop/voice_bench.py prepare --piper /private/venv/bin/piper \
  --voice /private/voices/en_US-lessac-medium.onnx --audio /private/voice-fixtures
python desktop/voice_bench.py run --candidate whisper-kokoro \
  --asr-model /private/faster-whisper-base --audio /private/voice-fixtures \
  --output /private/voice-results/whisper-kokoro --runs 5
python desktop/voice_bench.py run --candidate kyutai-pocket \
  --asr-model kyutai/stt-1b-en_fr --audio /private/voice-fixtures \
  --output /private/voice-results/kyutai-pocket --runs 5
```

For an already cached checkpoint without a `refs/main` entry, pass its exact snapshot directory with `--slm-model`; the SLM deliberately disables network lookup. Kyutai `--asr-model` also accepts a verified local directory containing `config.json`, `model.safetensors`, `mimi-pytorch-e351c8d8@125.safetensors`, and `tokenizer_en_fr_audio_8000.model`. Prefer normal-priority resumable HTTP/1.1 downloads into the root-disk Hugging Face cache over idle-priority writes on the pressured models mount. With `HF_HUB_ENABLE_HF_TRANSFER=0 HF_HUB_DISABLE_XET=1`, package-managed downloads also avoid accelerated-transfer workers. Under contention, defer GPU execution until `/proc/pressure/io` reports `some avg10 < 30`, checking no more often than every two minutes and abandoning the wait after three hours.

Run candidates serially on groot. The sampler records host CPU percentage (0–100 across the whole host), process CPU percentage (can exceed 100), RSS, total GPU memory, free memory, GPU load and power. The default minimum free-VRAM guard is 2 GiB; a breach stops the next stage/chunk. This is cooperative admission, not an allocator-level reservation: never run competing model loads or evict user processes. Exit the owned model process after each candidate. Record other GPU occupants separately and distinguish loaded-process memory from total card memory.

## Metric contract

`trials.json` preserves failures, transcript, response, micro-WER numerator/denominator, ASR and SLM durations, TTS first generated non-silent PCM, and total batch-EOS to first generated PCM. Fixture hashes are checked before inference. `resources.json` retains the sampled resource trace even when a model-loading stage fails; `failed-run.json` distinguishes a setup failure from a completed measurement. `summary.json` gives nearest-rank p50/p95 among successful turns **and both successful/eligible denominators**; five repeats is exploratory, not an SLA.

The local runner uses a batch-EOS boundary and a generated PCM sink, **not physical acoustic playback**. It does not implement an audio device, network, streaming input VAD, duplex playback, or tool receipt. It therefore reports barge stop and actual task correctness as **null/not qualified**, never zero or inferred from a quick model token. The keyword pass column is only a transparent lexical smoke rubric; it is not a substitute for evidence-backed Kevin-task correctness or a blinded human naturalness score. Interrupt/cancel utterances exercise recognition and response content, not real acoustic interruption.

The historical gpt-realtime comparison in `docs/BIDI-VERIFICATION.md` is actual synthetic WebRTC acoustic data from 2026-10-20 (question first audio p50/p95 2,756/5,356 ms; barge stop 329/407 ms, n=10). Keep it labeled **historical, different endpoint/transport, not a matched new A/B**. `STACK-VERIFICATION.md` instead reports first-text observations and must not be relabeled first audio. No new control spend is implicit in this runner.

Local provider marginal billed cost is zero because the adapters make no metered calls. Total operating cost/hour remains unknown without an electricity tariff and incremental-power baseline; null is not zero. No candidate is promoted from this partial sweep. Full promotion still requires matched acoustic playback and barge tests, truthful tool/native receipts, owner-approved handset route, longer soak, naturalness ratings, and enough samples for reliable tails.

## Feasibility gates

Stock Unmute's documented 16 GiB floor exceeds the shared 12 GiB card; do not start all stock services together. The measured lightweight Kyutai arm replaces heavyweight Kyutai TTS with CPU Pocket TTS and uses the smallest cached Qwen reflex model, recording those substitutions explicitly. Official Candle Moshi Q8 plus its default KV cache exceeds the co-resident budget; a community Moshi Q4 binary is a separate feasibility arm, not a silently equivalent model. Its offline `--bench` disables SDL I/O and measures frame throughput, **not** WER, TTFA, barge-in, or tool correctness. Keep those unavailable columns null.

## Measured overnight sweep — 2026-10-23

Results and raw synthetic trial/resource receipts: [voice-pareto-20261023](voice-pareto-20261023/). Both cascade arms completed eight fixtures × five repeats. All local latency values are batch-EOS→first non-silent **generated PCM**, not speaker playback.

| Candidate | PCM p50 / p95 ms | WER | Total-card peak MiB | Host CPU p95 | Process CPU p95 |
|---|---:|---:|---:|---:|---:|
| Kyutai STT→Qwen3-0.6B→CPU Pocket | 1,691 / 3,533 | 14.47% | 6,484 | 19.1% | 105.4% |
| faster-whisper-base→Qwen3-0.6B→CPU Kokoro | 2,804 / 9,429 | 10.53% | 4,022 | 21.3% | 153.6% |
| Moshi community Q4 | unavailable: throughput probe | unavailable | 7,435 | 22.1% | unavailable: sampler observes supervisor |
| gpt-realtime historical acoustic control | 2,756 / 5,356 **acoustic** | unavailable | unavailable | unavailable | unavailable |

Historical control: 2026-10-20, question stratum n=10, barge p50/p95 **329/407 ms**, recorded numbers only. Local barge latency is **unavailable**, because this harness has no duplex playback sink. No new control call, audio device, phone, live profile/service modification, or deployment occurred.

Moshi completed 125 silence frames in **5.133 s** (**24.354 frames/s**) after 14.385 s loading; model-reported device memory delta **4,806 MiB**. Its supported `--threads 0` is required with this release's CUDA backend: upstream `tools/common_ggml.h` incorrectly passes `backend` rather than `backend_cpu` to `set_n_threads_cpu` for positive thread values and aborts. No binary patch was made. Silence throughput is not EOS latency, WER, or a conversational cohort.

All runs used `nice -n10`, serial admission, a ≥2 GiB free-VRAM guard, and a supervisor stopping owned process groups for new external GPU demand/ComfyUI. Completed-run IO PSI admission values: Kyutai **25.96%**, Whisper **22.57%**, Moshi **27.98%** (all `some avg10 < 40`). Minimum free VRAM: **5,472 / 7,934 / 4,521 MiB**, respectively. Existing Garden Room (**1,492 MiB**) and Tern (**423 MiB**) occupants were untouched and remained the only GPU processes afterward. Total-card values include them. Host CPU is whole-machine percent; process CPU uses one-core=100% units.

Initial offline attempts exposed uncached Pocket tokenizer/weights and Kokoro voice assets. Corrected runs fetched missing assets into `/workspace/cache/z0live-hf`; Qwen/Whisper snapshots and all inference runtime/models reside on NVMe. Failed initial rows are not quality measurements and are excluded. Runtime: Python 3.12.13, torch/torchaudio 2.9.1, transformers 4.57.6, faster-whisper 1.2.1, kokoro 0.9.4, pocket-tts 4.0.1, moshi 0.2.13; full package inventory retained with the results.

**Recommendation:** neither cascade dominates. Kyutai/Pocket improves generated-PCM median/tail; Whisper/Kokoro improves WER and memory. Keep remote L0 until matched acoustic/barge and truthful task-effect acceptance. Provider billing is zero for local arms, but operating cost/hour is unknown. Keyword pass rates (50.0% and 83.33%) are not verified task correctness.

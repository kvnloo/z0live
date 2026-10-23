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

Run candidates serially on groot. The sampler records host CPU percentage (0–100 across the whole host), process CPU percentage (can exceed 100), RSS, total GPU memory, free memory, GPU load and power. The default minimum free-VRAM guard is 2 GiB; a breach stops the next stage/chunk. This is cooperative admission, not an allocator-level reservation: never run competing model loads or evict user processes. Exit the owned model process after each candidate. Record other GPU occupants separately and distinguish loaded-process memory from total card memory.

## Metric contract

`trials.json` preserves failures, transcript, response, micro-WER numerator/denominator, ASR and SLM durations, TTS first generated non-silent PCM, and total batch-EOS to first generated PCM. Fixture hashes are checked before inference. `resources.json` retains the sampled resource trace. `summary.json` gives nearest-rank p50/p95 among successful turns **and both successful/eligible denominators**; five repeats is exploratory, not an SLA.

The local runner uses a batch-EOS boundary and a generated PCM sink, **not physical acoustic playback**. It does not implement an audio device, network, streaming input VAD, duplex playback, or tool receipt. It therefore reports barge stop and actual task correctness as **null/not qualified**, never zero or inferred from a quick model token. The keyword pass column is only a transparent lexical smoke rubric; it is not a substitute for evidence-backed Kevin-task correctness or a blinded human naturalness score. Interrupt/cancel utterances exercise recognition and response content, not real acoustic interruption.

The historical gpt-realtime comparison in `docs/BIDI-VERIFICATION.md` is actual synthetic WebRTC acoustic data from 2026-10-20 (question first audio p50/p95 2,756/5,356 ms; barge stop 329/407 ms, n=10). Keep it labeled **historical, different endpoint/transport, not a matched new A/B**. `STACK-VERIFICATION.md` instead reports first-text observations and must not be relabeled first audio. No new control spend is implicit in this runner.

Local provider marginal billed cost is zero because the adapters make no metered calls. Total operating cost/hour remains unknown without an electricity tariff and incremental-power baseline; null is not zero. No candidate is promoted from this partial sweep. Full promotion still requires matched acoustic playback and barge tests, truthful tool/native receipts, owner-approved handset route, longer soak, naturalness ratings, and enough samples for reliable tails.

## Feasibility gates

Stock Unmute's documented 16 GiB floor exceeds the shared 12 GiB card; do not start all stock services together. The measured lightweight Kyutai arm replaces heavyweight Kyutai TTS with CPU Pocket TTS and uses the smallest cached Qwen reflex model, recording those substitutions explicitly. Official Candle Moshi Q8 plus its default KV cache exceeds the co-resident budget; a community Moshi Q4 binary is a separate feasibility arm, not a silently equivalent model. Its offline `--bench` disables SDL I/O and measures frame throughput, **not** WER, TTFA, barge-in, or tool correctness. Keep those unavailable columns null.

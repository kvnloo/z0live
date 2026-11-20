# Local voice baseline: Liquid LFM2.5-Audio-1.5B

The bundled Studio runner delivered first PCM in **68 ms median** on finalized speech clips (measured). That clears a model-side feasibility check, not a live turn-taking check. Measure endpointing and playback before training for lower latency.

## Studio results

All numbers in this table are measured or computed from measured samples. Times and real-time factors are median [min, max]. Peak RSS is the macOS process lifetime high-water mark (`wait4`), including model load and warm-up, not incremental memory or a separate GPU allocation total.

| Mode | Cases | First output | Complete response | Quality or throughput | Peak RSS |
| --- | ---: | --- | --- | --- | ---: |
| ASR | 20 | First text: 51 [44, 99] ms | Final transcript: 77 [63, 159] ms | WER 0.00% (0 edits / 213 reference words) | 1.314 GiB |
| TTS | 10 | First PCM: 25 [21, 25] ms | 455 [209, 766] ms | RTF 0.120 [0.116, 0.126]; WAV 3.82 [1.66, 6.62] s | 1.255 GiB |
| Interleaved speech-to-speech | 10 | First PCM: 68 [67, 75] ms | 972 [705, 1514] ms | WAV 7.50 [5.42, 11.74] s | 1.286 GiB |

The measured host was an arm64 Mac Studio (`Mac17,14`, Apple M5 Max, 36 GiB, macOS 27.2.1 build `26B131`). The bundle reported build `7641 (68d8edf2)`. Logs show Metal offload and CPU fallbacks in the audio encoder. The Metal tensor API probe failed, but the embedded Metal backend loaded and ran.

- **Fixtures:** Original English sentences and questions spoken by macOS `say`, alternating Alex and Samantha. Input WAVs were verified as mono PCM16 at 16 kHz, with measured ASR durations of 1.44–6.20 s. The TTS prompts were 5–24 words (measured counts) and used the US male voice. This is synthetic smoke coverage, not a natural-speech WER estimate. WER uses corpus word-level Levenshtein distance with lowercasing and punctuation removal, retaining internal apostrophes and without spoken-number equivalence rules.
- **Timing:** The local client starts the monotonic clock just before posting the prebuilt request to a warm runner. For speech, submission of the complete WAV is the file-based end-of-input boundary. There is no paced microphone input. First audio means the first nonempty decoded PCM event at the client, not an audio token or voiced playback. Completion means the stream's `stop` event. TTS RTF divides elapsed time to `stop` by generated WAV duration. Timings include loopback transport and request handling; they exclude recording, VAD, file synthesis, JSON preparation, model boot, WAV writes, and playback.
- **Isolation:** A separate loopback server per mode, serial requests, one unscored warm-up per mode, and `reset_context: true` per request (configured). No Studio voice service or phone was used. The leading load averages were 3.14 at start and 2.64 at finish (measured). No services were paused to make the Studio idle.
- **Checks:** All scored requests returned the expected modality and both `stop` and `[DONE]`. Output WAV headers, durations, and full sample counts matched the recorded data. The client rejects empty, silent, or non-finite output. Chat generated both text and PCM. See local logs and the linked per-case receipt.

A second full pass on the same fixtures measured 73 ms median final ASR, 25 ms first TTS PCM, 0.119 TTS RTF, and 68 ms first interleaved PCM. ASR again had 0 edits in 213 words. The second pass is a repeatability check, not an independent corpus.

The primary runner logs show 28–69 ms of audio encoding per ASR clip and 28–40 ms per interleaved clip, including warm-up (measured). The timed backbone evaluation alone took 58–215 ms for TTS and 222–473 ms for interleaved turns (measured). The encoder is a substantial part of first output after speech. Backbone evaluation is a substantial part of generating the full reply, before counting audio decoding and transport. These are stage timings, not a CPU or GPU saturation profile.

As an untimed check, the same model in ASR mode recovered every normalized word in the 10 primary TTS WAVs and differed by 2 words across the 10 chat WAVs compared with streamed text (measured, not an independent naturalness judgment). Two chat replies were plainly wrong: red plus white gives pink, not yellow, and the ocean between California and Japan is the Pacific, not the Sea of Japan. The fast turn metric does not score answer quality.

### What the comparison means

[HILLCLIMB-OVERNIGHT.md, Track A](HILLCLIMB-OVERNIGHT.md#track-a-conversation-studio-real-calls) reports GPT-Live at 0.94 s and 0.96 s median in later live replay runs (historical measurements, about 0.95 s), versus 1.30 s for the original baseline (historical measurement). Liquid's 68 ms measures a smaller segment on different input: finalized WAV submission to PCM delivery. GPT-Live's gap is a replay turn-taking metric. Endpoint detection, input buffering, output buffering, and first voiced playback can change the result. The numbers do not establish a live speedup. The local result supports testing an isolated live adapter before changing model weights. Neither Android speed nor live turn-taking was measured here.

## Run it again

Both installed ZIPs contain the runner, shared libraries, and license files, but neither contains a README. Use the [Liquid GGUF model card](https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B-GGUF), the bundled macOS binaries' `--help`, and the [runner source linked by Liquid](https://github.com/ggml-org/llama.cpp/pull/18641). The bundle is not a generic stock `llama-cli`. On Android, use the supplied arm64 binaries with their `.so` files beside them and that directory on `LD_LIBRARY_PATH`; this is the inferred launch setup, not a device test.

Run the benchmark from the worktree with Python 3.9 or later (the Studio used 3.9.6, measured). Pick a new output directory outside the worktree. `--smoke` limits the run to one scored case per mode.

```sh
python3 voice-adapter/bench/voicebase.py "$HOME/.local/share/z0live/bench/voicebase-rerun"
```

The script uses installed models only. It passes `--offline -c 2048 -ngl all -s 42 --perf` (configuration, not measurements), binds a private loopback port, and stops its own subprocess. The bundled server takes `POST /v1/chat/completions` with `stream: true`. Send text as the user content or base64 WAV in an `input_audio` part. The tested bundle sends text in `delta.content` and float32 little-endian PCM in `delta.audio_chunk`, which the script writes as PCM16 WAV. Later runner source is not a wire-format guarantee.

For one-shot CLI use, put the four filenames behind the same flags as the server: `-m` for the base GGUF, `-mm` for `mmproj-`, `-mv` for `vocoder-`, and `--tts-speaker-file` for `tokenizer-` (the audio detokenizer). Keep the libraries beside `llama-liquid-audio-cli`. Append these arguments by mode:

| Mode | Arguments |
| --- | --- |
| ASR | `-sys 'Perform ASR.' --audio input.wav` |
| TTS | `-sys 'Perform TTS. Use the US male voice.' -p 'The kettle is ready now.' --output output.wav` |
| Speech-to-speech | `-sys 'Respond with interleaved text and audio.' --audio input.wav --output output.wav` |

The Studio receipt directory is `~/.local/share/z0live/bench/voicebase-20261120-final/`. It contains `asr.log`, `tts.log`, `chat.log`, `results.json`, input WAVs, and generated `tts-01.wav` through `tts-10.wav` and `chat-01.wav` through `chat-10.wav`. WAVs and models stay off Git. The checked-in [per-case result JSON](VOICE-LOCAL-BASELINE-20261120.json) contains only benchmark metadata and synthetic text.

## Fine-tuning path and license

Liquid's [training README and examples](https://github.com/Liquid4All/liquid-audio/tree/19e65845923a7f136442c95137884ec61eb386aa#finetuning) document sequential and interleaved supervised fine-tuning. Start from the [bfloat16 PyTorch model](https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B), not the Q4_0 files. Map each example to a `list[ChatMessage]` with system, user, and assistant roles. Use `TextSegment`, `AudioSegment` with audio bytes, or `InterleavedSegment` with text and audio. `LFM2AudioChatMapper` maps these to text tokens, input audio features and lengths, output audio codes, modality flags, and a supervision mask. Liquid's preprocessing saves a Hugging Face Dataset to disk. `LFM2DataLoader` loads it for `examples/train.py`. `examples/preprocess_jenny_tts.py` demonstrates text-to-audio preparation.

The shipped trainer uses Accelerate, BF16, and fused AdamW over `model.parameters()`, with no LoRA option. This is full-model optimization of the trainable parameters, not retraining the separate audio codec or detokenizer. Generic runner `--lora` help and LoRA in the vendored Kyutai Moshi code do not verify a Liquid Audio LoRA training or export path. Treat adapter training and merging as separate engineering work. The trainer saves Accelerate checkpoints. Before a full run, prove checkpoint reload and export of the applicable GGUF components with the matching Liquid runner converter, then compare PyTorch and GGUF outputs. Training and conversion were not run here.

**Compute (guessed, not measured):** Start with one local BF16-capable CUDA GPU with 48 GiB for a small full-model smoke run. Use a small batch and short contexts. Measure peak allocated memory and step time before budgeting a larger run. Use gradient accumulation or sharding if needed. The Studio's inference RSS does not predict training memory. The stock CUDA-oriented path has not been validated on Apple MPS.

Both [GGUF weights](https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B-GGUF/blob/main/LICENSE) and the [PyTorch checkpoint](https://huggingface.co/LiquidAI/LFM2.5-Audio-1.5B/blob/main/LICENSE) use the LFM Open License v1.0 (sourced license identifier). It permits modification and redistribution under conditions. Commercial use has an annual revenue threshold defined as USD 10 million or more (sourced license term), applied to the legal entity as defined in the license. Section 5 says entities that exceed the threshold lack commercial permission and has a qualified-nonprofit research provision. Confirm exact-threshold and commercial cases with legal or Liquid before deployment. Redistribution requires the license, prominent notices on changed files, and retention of applicable notices and attribution. Preserve third-party terms, including NeMo's Apache-2.0, Kyutai code's MIT, and the applicable CC BY 4.0 source checkpoint notices (sourced license identifiers). Verify corpus and speaker rights separately. The packaged runner's MIT license does not override the weight license.

## Proposed training and evaluation

1. Freeze a public or synthetic held-out suite first. Add natural clean/noisy speech and accent coverage, plus scripted dialogues with within-turn pauses, deliberate interruptions, backchannels, and handoffs. Keep speakers, scripts, and recording sessions disjoint across splits. Use rights-cleared public recordings or purpose-made consented speech. Exclude Telegram, Hermes history, owner messages, and private call traces.
2. Establish the untuned model with the same model format, audio format, request path, and endpoint policy as the candidate. Measure last voiced input, end-of-turn decision, first PCM, and first voiced output on one monotonic clock in a local replay. Measure transcript start/final, TTS RTF, peak RSS, and failures, with repeat runs and median, tail, and spread. Compare hosted live numbers only on a matched, separately authorized replay.
3. Decide what to train from the errors. Use paired speech/transcript for ASR, text plus licensed target speech for TTS, and timed user/assistant audio plus aligned response text for interleaved chat. Keep a provenance manifest with speaker/session, rights, prompt, transcript, audio offsets, and pause/interruption labels. Start with a small rights-cleared subset. Confirm training loss, checkpoint reload, and quantized export before scaling the corpus. Keep endpoint control outside the model unless a separate streaming policy test proves otherwise.
4. Gate each candidate on normalized WER plus TTS spoken-text fidelity from an independent local ASR, and naturalness from a version-pinned offline UTMOS model or blinded local listener preference. Treat predicted MOS as a proxy, not a human MOS. Freeze judge revision, resampling, loudness normalization, and clip order. Evaluate turn-taking on false cut-ins, missed takes, overlap duration, backchannel usefulness, and barge-in cancellation. Keep a candidate only if held-out quality improves beyond repeat noise without regressing latency, memory, or cutoff rates. Keep the current GGUF for rollback.

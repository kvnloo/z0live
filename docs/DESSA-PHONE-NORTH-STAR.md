# Dessa-quality, fully on-phone voice — z0live north star (research, 2026-10-26)

> **Status: design and experiment triage only.** No model or app has been installed; no microphone, audio, device, live service, paid API, or physical benchmark was exercised for this document. This is an aspirational target, **not evidence of achieved parity**.

## Product contract

**North star:** Make z0live feel as natural, expressive and continuously attentive as [Block's Dessa](https://block.xyz/dessa), **on a Samsung Galaxy S25 Ultra, in airplane mode and with no mandatory desktop/cloud inference**. Target low latency, backchannels, natural pauses, interruption, laughter/prosody, correction, and sustained hands-free interaction. The S22+ is a constrained fallback tier. Block publicly describes Dessa as audio-in/audio-out, speech-only-trained, simultaneous listen/speak and real-time inference on a **MacBook Pro M4 Max or better**; it has **not provided a phone result or downloadable weights** as of this review. This is a *reference UX*, not a component ready to integrate.

A fast STT → LLM → TTS cascade is **the first deployable ladder rung, not equivalent to speech-native Dessa**. Text erases some affect/timing and normal turn-segmentation is not simultaneous full duplex. To reach the north star, preserve a continuous acoustic side channel, a floor controller that sees both microphone and speaker PCM, and a later small speech-native model/distillation research branch.

**Architectural boundaries remain unchanged:**

- z0intelligence selects/pins an immutable VoicePlan (compute placement, model, resource budget). Do not add a second selector.
- z0live ConversationActor owns duplex floor, media epoch/cancellation, barge-in, backchannels, attention, replay and acoustic observability. There is **one** authoritative local audio actor.
- The existing Android/Lawnchair voice entry owns mic, echo cancellation, playback and instantaneous audio stop. Reuse, don't silently replace, the portable actor donor [PR #6](https://github.com/kvnloo/z0live/pull/6), Android/PCM donor [PR #7](https://github.com/kvnloo/z0live/pull/7), or the broker's separate HTTP contract.
- Hermes/OMP are still harness-native authorities when available. Deep thinking, remote memory, tool results, approvals and task cancellation stay asynchronous and distinct from speech playback. **Offline phone conversation works without them; disconnected phone cannot falsely imply that remote actions completed.**
- Preserve #1 as RFC, #8 as work allocation, #32 as P0 product parity, #44 as local-voice Pareto/evidence. This document adds no competing issue queue.

## Evidence available today (different measurement boundaries)

| Evidence | Grounded observation | What it does NOT prove |
|---|---|---|
| [Dessa by Block](https://block.xyz/dessa) | Block reports natural expressive, speech-native full duplex; local M4 Max real-time | Released OSS weights, S25 feasibility, open benchmarks |
| [z0live #44 result comment](https://github.com/kvnloo/z0live/issues/44#issuecomment-6360599971) / [draft #47](https://github.com/kvnloo/z0live/pull/47) | **40/40 synthetic trials per local cascade on groot.** Kyutai STT → Qwen3-0.6B → CPU Pocket: EOS → *generated PCM* median/p95 **1,691/3,533ms**, WER **14.47%**; faster-whisper-base → Qwen3-0.6B → CPU Kokoro **2,804/9,429ms**, WER **10.53%**. Moshi community Q4 produced **24.354 frames/s** on silence-only probe. | No phone performance, physical first audible speech, barge safety, real acoustic comparison, or Dessa-like emotion |
| [#44 Oct20 historical remote control](https://github.com/kvnloo/z0live/issues/44#issuecomment-6359352456) | GPT realtime: first **acoustic** audio p50/p95 **2,756/5,356ms**, historical barge p50/p95 **329/407ms** (n10 acoustic stratum). | Not matched to #47 local synthetic generated PCM or current production A/B |
| [voxedge](https://github.com/GT-KIM/voxedge), original author GT-KIM | Snapdragon SM8750 phone reportedly performs **airplane-mode** VAD→ASR→Qwen3-4B/Genie→clause-streamed Supertonic TTS; *recognized text → first audible* **0.55–0.66s**, not utterance-EOS → audible. | Continuous full duplex; its barge-in is experimental **off by default** owing to echo self-interruption. Qualcomm proprietary runtime is a separate licensing/deployment dependency. |
| [Gradium Phonon](https://gradium.ai/blog/gradium-releases-phonon), runtime by gradium-ai | **2026-10-21**: 31M English Phonon, 64.5MB q8, **Apache-2.0** model; native Rust/C/Android Kotlin interface, CPU Android or QNN NPU with SoC-specific bundles, streaming PCM and stop API. Vendor reports better blind preference vs Kokoro/Pocket. | No independent S25 TTFA/p95, emotional responsiveness, or a dialogue model. **40M variants are CC BY-NC 4.0**. |
| [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts/releases/tag/v4.0.0) by Kyutai | **2026-10-20**: 18.2M, 6.9M and experimental 1.9M English variants, plus reward-fine-tuned versions; small/CPU-friendly, streaming; [sherpa-onnx Android/Kotlin](https://github.com/k2-fsa/sherpa-onnx) and [Pocket-TTS-LiteRT](https://github.com/john-rocky/Pocket-TTS-LiteRT) ecosystem. | Original ~200ms first-chunk claim was on an Apple CPU, not S25; small checkpoint quality, licenses/voice conditions and Android throughput require review. |
| [Parakeet realtime EOU 120M](https://github.com/mudler/parakeet.cpp), originally NVIDIA | Small native streaming RNNT/EOU candidate; ONNX Android examples via [soniqo/speech-android](https://github.com/soniqo/speech-android); separate Silero VAD acoustic reflex. | Accurate barge classification in acoustic echo, S25 sustained WER and EOU cut-in rates remain untested. Verify NVIDIA model license independently. |
| [TYGER](https://huggingface.co/nineninesix/tyger), NineNineSix | Open-weight learned floor-state/barge/backchannel controller. **652M end-to-end** (638M Nemotron ASR + 14M GRU), 320ms inference hops, microphone and playback input. | Not a 14M standalone phone model, not production-grade barge proof; OpenMDW-1.1 weights require legal review. |
| [Moshi](https://github.com/kyutai-labs/moshi), [PersonaPlex](https://research.nvidia.com/labs/adlr/personaplex/), [Realtime-Venus](https://github.com/inclusionAI/Realtime-Venus), [Lychee-FD](https://github.com/HITsz-TMG/Lychee-FD) | Reference speech-native full-duplex training, native audio output, backchannels and asynchronous-task separation. | Their multi-billion-parameter implementations have no verified sustainable Galaxy S25 real-time duplex result. Don't mistake a desktop demo for a phone build. |
| [Hugging Face speech-to-speech](https://github.com/huggingface/speech-to-speech) | Reusable open-model host cascade; core OpenAI Realtime **WebSocket/WebRTC** protocol compatibility and measurable pipeline stages. | Host-first, microphone/echo limitations; protocol is a core subset, not full equivalence or proven Android quality. |

**Triage priority, dated 2026-10-26:**

1. **P0: preserve current parity gate, fix evidence instrumentation** ([#32](https://github.com/kvnloo/z0live/issues/32)). Distinguish **Dash Expo chunked → CPU STT** from **Lawnchair direct WebRTC**; they are not the same product path. Instrument physical playback and speech-start/end on the same phone/route. Fix omitted/misrecognized utterances and stale replay before provider promotion.
2. **P1: Phonon 31M vs Pocket 18M/7M** streaming Android TTS *in isolation* (offline, permissions-gated). Phonon is first candidate due released Kotlin/C native stop/stream API + permissive 31M weight license, with Pocket as smaller resource-control option; compare naturalness blinded, code-switch/numbers, first PCM, first audible, RSS, energy and thermals. Investigate smaller models only if speaker quality remains acceptable.
3. **P1: Parakeet 120M EOU / sherpa-onnx + Silero and a quantized small model (Qwen3 0.6B→4B / Gemma class)**. Reuse #45's VoicePlan-provider seam but **do not pretend its buffered WAV transcript/control HTTP interface is true streaming or on-device**. Prefer Android native IPC/adapters reusing donor #7. Stream clause fragments into TTS instead of waiting for full text.
4. **P1: genuine full-duplex floor/AEC**. One mic owner, separate input/reference-output streams, hardware/OS echo cancellation, double-talk detection, local speaker silence on barge, epoch-safe discard of already queued PCM and future synthesis chunks; reason independently about worker cancellation. Continuously listen even while speaking. Reserve acoustic features (intonation, energy, timing, laughter) alongside ASR; no invented emotional classifier accuracy.
5. **P2: learn audiovisual/speech-native turn/affect head**. Reproduce TYGER-style dual-audio floor decisions offline using our fixture corpus; compare deterministic baseline to learned residual. Only after P0/P1 qualify a 1–2B native speech-codec candidate, distillation or audio-language fine-tuning on **licensed/consented** audio. Moshi/PersonaPlex/Venus on groot are upper-bound references, **not assumed phone models**.

## Candidate Android inference layout (hypothesis, not a deployed pipeline)

~~~text
S25 / native foreground session
  AudioRecord / AAudio mic frames (10-20 ms)
           |                 +-- microphone acoustic features (pause, pitch, affect)
       WebRTC AEC3 + NS <----+-- reference PCM from AudioTrack
           |                 |
  Silero VAD + streaming Parakeet-EOU-120M
           |       \          |
      partial tokens  floor arbiter (listen/hold/backchannel/speak/interrupt)
           |          |      |
  immutable VoicePlan |    PCM playback flush + generation epoch cancel
           |          |
  local resident Qwen/Gemma quant LLM
           | short semantic clauses (no unapproved tools)
  Phonon-31m (primary) | Pocket-18m/7m (challenger) streaming PCM
           |
     native AudioTrack / AAudio speaker --------------> AEC reference
           |
    when online: existing Hermes/OMP authority for async deep tasks
    when offline: only explicitly local actions; never fake receipts
~~~

Resource hypotheses: Run ASR on CPU/NNAPI, 2–4B SLM on GPU/NPU where supported, TTS on idle CPU or QNN, with capped allocations and a priority policy so the audio thread cannot starve. Admission measures **total process PSS/RSS, DSP/GPU/CPU reservations, power, thermals and throttling**; raw model bytes alone are not enough. Phonon QNN requires SoC-compiled graphs and proprietary Qualcomm runtime; Phonon CPU is the reproducible baseline. Avoid depending exclusively on proprietary QNN for the "OSS on-phone" claim; document the runtime distinction.

**Important:** Model selection belongs to z0intelligence/VoicePlan, not a hidden second z0live router. Never run two recognizers on a second mic capture path; both can consume shared, timestamped PCM if admission allows.

## Test contract (pre-register before listening to results)

Suggested **aspirational** S25 targets, not measured/release guarantees:

| Signal | Desired threshold / acceptance |
|---|---|
| Privacy | Fully local everyday conversation in airplane mode, after model provisioning; no network path without opt-in |
| EOS → first *audible* meaningful response | p50 ≤ 500 ms; p95 ≤ 1,000 ms; distinguish reflex/backchannel from substantive answer |
| Valid barge → *acoustic silence* | p95 ≤ 150 ms for built-in speaker, separate BT/headphone targets after measuring routing; no stale PCM, no duplicate reply |
| Missed/spurious turn handling | Track denominators and error classes; no unacknowledged real user turn, no repeated stale spoken reply or accidental authorized action |
| Conversational dynamics | Correct listen/hold/ack/yield/interrupt decisions, overlapping laughter/gasps/short acknowledgments, natural pause timing; no mandatory forced VAD-end between every utterance |
| Sustained use | ≥ 30 minute talk session on S25 with no OOM/crash, audio glitches, thermal shutdown or unacceptable energy drain; explicit measured temp/battery/perf curves |
| Lower tier | S22+ must pick a bounded smaller VoicePlan or explicitly fail/admit less capability; no claim of S25 parity |

**Event clocks** (monotonic timestamps, route+source SHA+runtime fingerprint; raw private media kept local): audio capture onset, detected user speech start/end, ASR partial/commit/EOU, model first token, TTS first generated PCM, output queue admit, **physical audible onset**, user barge onset, **physical output silence**, audio-device route changes, worker queued/running/cancel_requested/terminal. Keep historical external WebRTC control, synthetic PCM, hardware mic/speaker and headless text results in distinct strata. Barge tests require playback echo and double-talk, not synthetic VAD alone.

**Pre-register a fixed suite** adapted from #6/#47 and z0evals #86: (a) quick question, (b) long thought + pause, (c) backchannel mid-user-speech, (d) loud laugh / intonation change, (e) mid-agent correction and user barge, (f) simultaneous speech and echo, (g) background TV/music, (h) Bluetooth route switch, (i) canceled Hermes work and late result, (j) 30-minute session. 20+ randomized repetitions per scenario per arm where possible, same device and capture routes; report p50/p95 and confidence/error counts. Human blind preference A/B for naturalness vs OMP/live and licensed Dessa reference **only for UX**, never claim official Dessa benchmark parity.

**Failure gating**: replay, permission boundary, native-worker cancellation or speech self-interruption regressions block any candidate regardless of mean latency. Existing [#32](https://github.com/kvnloo/z0live/issues/32) release gate still applies. A high-quality one-shot TTS demo, offline decoder speed, or a model benchmark cannot pass the product gate.

## Bounded executable slices (each separate only after board #8 assigns owner)

| Order | Slice | Files/owner boundary | Done when |
|---|---|---|---|
| A | Instrument real audio and OMP/Lawnchair parity | Existing #32 and #44 tracing; **no new provider** | Matched S25 physical onset/EOS/playback/barge traces and failure taxonomy, with owner-approved opt-in |
| B | Native Android TTS feasibility, no speaker overlap | Isolated TTS adapter behind z0live PCM interface; Phonon-31m and Pocket tiny model | Same-device offline TTFA, RTF, memory, thermal and blinded A/B; zero remote host requirement |
| C | Native Parakeet EOU + local small SLM + clause streaming | Reuse #7 Android actor, #45 VoicePlan/local seam; no second actor | Flight-mode text-to-first-physical-audio waterfall, correct tool/safety receipts, cancellation |
| D | Always-listening playback, acoustic echo/barges | z0live floor and Android native AudioRecord/Track, deterministic epoch owner | Correct mic/speaker simultaneous operation and no self-barge, with measured physical silence timing |
| E | Prosody-aware/speech-native branch | Independent research behind pinned experimental VoicePlan | Demonstrates marked blind human naturalness improvement **without** regressing A–D metrics |

Reuse, don't supersede: [portable actor PR #6](https://github.com/kvnloo/z0live/pull/6), [Android PR #7](https://github.com/kvnloo/z0live/pull/7), [local seam draft #45](https://github.com/kvnloo/z0live/pull/45), [Pareto draft #47](https://github.com/kvnloo/z0live/pull/47), [Lawnchair PR #14](https://github.com/kvnloo/lawnchair/pull/14), [z0evals #86](https://github.com/kvnloo/z0evals/pull/86). Owner/reviewer consent, claimed files, contributor credit, exact-head evidence and work authorization remain mandatory. **No installs, ADB, live mic, model downloads, model spend or service changes without separate authorization** per #1/#32.

## External-source credit and license guard

This design stands on Block/Dessa as the aspiration; original authors/teams **Kyutai** (Moshi/Pocket), **Gradium** (Phonon/xn-ptts), **GT-KIM** (voxedge), **NVIDIA** (Parakeet, PersonaPlex, Nemotron), **k2-fsa** (sherpa-onnx), **NineNineSix** (TYGER), **inclusionAI** (Realtime-Venus), **HITsz-TMG** (Lychee-FD), **Hugging Face** (speech-to-speech), **soniqo** (speech-android) and their upstream contributors retain credit. Specific code/weight/runtime terms **are not interchangeable**: Phonon 31M weights Apache-2.0 vs 40M noncommercial, Pocket checkpoint and voice access conditions, NVIDIA Open Model License, TYGER OpenMDW-1.1, QNN proprietary runtime, native codec dependencies. Validate each pinned revision and redistribution rights before packaging an APK. No third-party code is copied in this document.

### Decision

**Commit to fully offline, Dessa-like S25 quality as the long-range product objective**, but implement **acoustic parity + a phone-native Phonon/Pocket/Parakeet cascade first**. Keep speech-native, expressive, turn-taking and audio-prosody research as explicit required subsequent milestones, not "done" when ASR/TTS is wired. Remain offline-first, transport-independent and strict about human tool authority.

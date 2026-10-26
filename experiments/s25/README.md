# S25 Ultra REA + adaptive voice qualification

**Target only:** Galaxy S25 Ultra SM-S938U, Snapdragon 8 Elite SM8750, Adreno 830, Hexagon HTP v75, nominal 12 GiB shared RAM. Do not extrapolate results to other phones. The owner wants Dessa-quality full-duplex **offline** speech. This is a scoped investigation, not a release, model-install, recording or ADB authorization.

## Verified execution, 2026-10-26

[Actions run 40278791185](https://github.com/kvnloo/z0live/actions/runs/40278791185) at branch head 404e8970e810a190c7b1114c205cba46e554adc7 executed 15/15 negative-control and fake-transport tests. On Ubuntu Linux with a pinned rea-agents@6.8.0 CLI, genuine REA inspect-model-artifact --hash full PASSED for a synthetic safetensors fixture. The independent rea doctor / inspect-adb-client calls FAILED due to incomplete optional host prerequisites and no ADB client. No real device, NPU inference, TTS or audio test ran. Do not turn simulated S25 facts into measurements.

**Real-model follow-up:** source-pinned Gradium Phonon q8 model GGUF SHA-256 a7d9aad2cd6dc66473f24a08bc6d2939030ef4d751ef82349361fbef3272dbae was verified on an ephemeral runner. The independent read-only tensor-header audit found **185 declared tensors, 17 names exceeding GGUF's 64-byte format cap, longest 76 bytes**. REA 6.8.0 correctly returned target_unavailable / GGUF name exceeds its format byte limit and retained partial Evidence, rather than fabricating a full model inspection. See [Actions run 40280264223](https://github.com/kvnloo/z0live/actions/runs/40280264223) and [official GGUF specification](https://github.com/ggml-org/ggml/blob/master/docs/gguf.md). This is a **model artifact representation incompatibility for REA and strict GGUF consumers**, **not** evidence that Gradium's specialized Phonon inference runtime fails. The QNN Android bundle is a different compiled artifact and must be tested separately. The workflow asserts this exact expected refusal; unexpected failures remain failures. No phone touched.

Older z0live [#44](https://github.com/kvnloo/z0live/issues/44) 40/40 local cascade trials ran on **groot desktop**, not the S25; first generated PCM is not first **audible** speech. Prior S25 work documented ADB pairing and app-level qualification, not a trustworthy quantified CPU/GPU/HTP inferencing comparison.

## REA protocol (REA by morluto)

[REA source](https://github.com/morluto/rea) offers evidence-backed local reverse engineering of model GGUF/safetensors, Android APKs (with a supplied JADX/JDK), native libraries, and ADB device properties (with supplied Android platform-tools). It is an inspector, **not** an inference/runtime optimizer. Its prompts investigate_feature, compare_application_versions, verify_reconstruction, trace_crash, audit_residual_unknowns, prepare_bounded_process_capture propose bounded investigations rather than run hardware by themselves.

Dry-run; no ADB or device access:

~~~bash
python3 experiments/s25/rea_protocol.py --phone
python3 experiments/s25/android_snapshot.py
python3 -m unittest discover -s experiments/s25 -v
~~~

Static model inspection on a machine with a working REA binary, no phone required:

~~~bash
python3 experiments/s25/rea_protocol.py --execute --model-file /absolute/path/to/model.gguf
~~~

On the owner's **groot** with its already-verified, identity-pinned phone-adb wrapper, **only during an owner-authorized read-only S25 diagnostic slot**:

~~~bash
phone-adb status
python3 experiments/s25/android_snapshot.py --execute
python3 experiments/s25/rea_protocol.py --execute --phone
~~~

Both runners are inert without --execute, reject unverified phone identity, have fixed bounded command sets, preserve private raw observations locally as mode 0600, and publish only observation digests/status. No ADB pairing, installs, UI driving, microphone/capture, APK extraction, system-settings change or streaming service actions. The snapshot checks model == SM-S938U before reading CPU, RAM, limited KGSL GPU busy (if accessible), thermal and battery data; NPU/GPU execution remains **unknown**. A declared system feature never establishes usable delegate capacity. Keep private phone logs, serial, addresses and audio out of public issues and CI.

REA model inspection supports GGUF and safetensors, not opaque compiled QNN .bin contexts. On a controlled owned APK, use JADX/REA to trace JNI, audio thread/AudioTrack ownership and references to actual QNN loader; prefer build-source analysis over disassembling others' protected applications. This stage does **not** replace real NPU trace or acoustic experiments.

## Exact S25-first OSS candidates and their evidence boundaries

| Role | CPU | GPU | Hexagon HTP NPU | Test requirement |
| --- | --- | --- | --- | --- |
| Small Qwen3 0.6B/1.7B/4B Q4 SLM | ARM64 llama.cpp | Adreno Vulkan/OpenCL when native build says so | PocketOrca/llama.cpp Hexagon if actual delegate+quant work | Same model revision, prompt, seed and quant; backend logs, TTFT, tok/s, PSS, throttling |
| Streaming Parakeet 120M ASR, Silero VAD/EOU | sherpa-onnx/ONNX Runtime CPU baseline | only after real backend support is shown | QNN only with confirmed compiled graph and license | Matched WER, end-of-utterance false/missed rate and 20-ms frame p95 |
| Phonon 31M TTS vs Pocket 18M/7M | upstream Phonon CPU / Pocket CPU reference | optional proven Vulkan | **Phonon-31m SM8750 precompiled QNN bundle** | Actual native stream PCM, AudioTrack audible onset, QNN execution trace, 30min energy/temp |
| Full duplex floor/AEC | Android audio HAL/WebRTC AEC3 | not presumed | not presumed | Real microphone + speaker overlap, acoustic silence after barge, echo self-interruptions |

**Concrete match:** [Gradium's Phonon Android QNN](https://github.com/gradium-ai/xn-ptts/blob/main/qnn/android/README.md) has a compiled SM8750 context and a native Kotlin PCM streaming/cancel API. Release asset phonon-31m-en-qnn.zip from 2026-10-20 is **308 MB, SHA-256 9da103025d88682428e350f7d65254bfa672d671a9a7e643fb4527e370007722**. Requires Android >=12 ARM64, compatible firmware and **QNN runtime/QAIRT 2.50.0**, matching runtime/skels, local Maven-built unpublished AAR and correctly aligned context metadata. **No automatic CPU fallback.** Even JNI_PROBE_OK from Gradium's smoke app checks packaging, **not HTP inference**. So match Build.SOC_MODEL == SM8750 and prove actual NPU execution rather than editing binary metadata to spoof support. Weight licensing for Phonon31M Apache2 is distinct from Qualcomm runtime licensing. No assets have been downloaded to the S25.

For REA static CPU artifact triage: pinned upstream [Phonon q8 GGUF](https://huggingface.co/gradium/phonon-31m-en) at revision 52433c46975bfa13144c56204c9f4ee1c5b148aa, full SHA-256 reportedly a7d9aad2cd6dc66473f24a08bc6d2939030ef4d751ef82349361fbef3272dbae (verify original bytes). Generic llama.cpp **does not** decode Phonon's specialized GGUF model merely because it has GGUF metadata.

[Original PocketOrca-LLM authors](https://github.com/PocketOrca/PocketOrca-LLM) report Snapdragon8Elite/Adreno830/HTP v75 inference on Galaxy S25, roughly **23–30 tok/s 1B Q4, 10 tok/s 7B Q4 NPU on their own device**; these numbers are **not ours**. Its OpenAI-compatible endpoint is a promising isolated CPU/GPU/NPU benchmarking donor but it has considerable app permissions, downloaded model requirements and proprietary Qualcomm dependencies; don't alter the daily-driver phone without separate approval. [llmfit](https://github.com/AlexsJones/llmfit/blob/main/docs/platform-support.md) does not autodetect Adreno in Android/Termux/PRoot; memory overrides only influence suggestions, not actual GPU execution. [Unsloth](https://github.com/unslothai/unsloth) can help tune/export models elsewhere but does not by itself demonstrate compatible phone GPU/NPU inference.

## Adaptive z0live placement — no second router

[Canonical z0intelligence issue #45](https://github.com/kvnloo/z0intelligence/issues/45) owns hardware-aware eligibility, Pareto selection and **immutable VoicePlan**. z0live owns exactly one mic/PCM/floor actor, cancellation epochs, physical audible evidence, ASR+TTS adapters and callbacks. Hermes/OMP remain action authority when connected. Phone-local conversation must work without remote Hermes or a secretly required server; unknown remote task outcomes cannot be spoken as completed.

Qualify, **in the SAME S25 profile**, each role using stable artifact SHA, actual delegate output, prefill/decode metrics, memory PSS + Android MemAvailable/LMKD, CPU/GPU/HTP reports, p50/p95/p99 first PCM and physical first audible, thermals under 5/30-minute sustained loads. First rank hard eligibility (model license, RAM headroom, Android AEC, offline consent, correct output), then keep a Pareto frontier (latency, naturalness, WER, heat, battery). Don't sum reported CPU+GPU+NPU memory or interpret advertised 12GiB as free model RAM. Qualcomm may compile a graph for one quant/backend and not another.

Failure/deoptimization: reject wrong device/model/driver, stale receipts, unavailable delegate, OOM, thermal throttling or audio-thread glitch. On invalidation, cancel at current speech epoch, flush queued PCM and allow z0intelligence to choose **only a previously qualified explicit fallback** at a safe boundary; never silently swap NPU→CPU while claiming HTP performance. Audio floor stays active. Tool authority must not repeat an effect on retry. Optionally model rollback to a smaller local SLM/TTS, not mandatory cloud.

## Next real-device acceptance stages (currently NOT RUN)

1. **A/read-only approved inventory:** pinned phone identity, true SOC and RAM; REA ADB host preflight, models and native libraries. This is the only stage these scripts can execute.
2. **B/isolated native canary, separate approval:** Gradium Phonon SM8750 QNN vs CPU, Pocket tiny CPU, PocketOrca small Q4 CPU/GPU/HTP *on the identical handset*. Record exact runtime/weight/delegate and fail on hidden fallback; never install over the primary launcher.
3. **C/acoustic and thermal qualification, separate approval:** end-user-speech→**speaker audible** response p50/p95, barge onset→acoustic silence, lost utterance count, EOU accuracy, echo/noise/Bluetooth; battery draw, thermal throttling and LMKD for 30min. Use OMP/live control; physical output, generated PCM and synthetic fixture timing must stay separate. Blocking [z0live parity #32](https://github.com/kvnloo/z0live/issues/32) applies.

Credit: morluto/REA, Gradium/Phonon, PocketOrca, Kyutai, NVIDIA, k2-fsa, llmfit and Unsloth authors remain the origins of their tools and models. Preserve weights, license, Qualcomm SDK and voice provenance; no third-party code copied in this document.

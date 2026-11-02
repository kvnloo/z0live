# z0live voice setup · Android / HyprSpaces

The voice setup page is at `/voice-setup.html` after the normal Vite build. It is a **preference and handoff UI**, not a login portal, model installer, audio transport, or hardware benchmark. The browser stores no credential or device probe. It exports `z0live.voice_preference.v1`, **not** an admitted `z0int.voice_plan.v1`.

## Routes
- **Hybrid (preferred for Galaxy S25 Ultra):** keep the working Tern → Hermes/OMP → Wi-Fi ADB path, with Hermes `phone-harness` for structured phone actions and per-call approval. A future secure phone-to-host audio transport is an independent gate.
- **ChatGPT consumer app:** open the installed app and sign in through the official UI. No third-party consumer ChatGPT Voice login API is assumed.
- **Google/Gemini assistant:** Android system assistant handoff only. Lawnchair's existing Assistants screen uses `ACTION_ASSIST`.
- **OpenAI Realtime developer API:** a separately configured **host** and independently billed API, not a ChatGPT subscription. Never embed a secret in an APK/browser.
- **Android on-device ASR:** check `SpeechRecognizer.isOnDeviceRecognitionAvailable`, then evaluate sherpa-onnx QNN demo builds with the correct chipset, model license, language, storage and battery constraints. Qualcomm QNN accelerates the NPU, not necessarily the Adreno GPU.
- **Moshi/PersonaPlex:** run on a compatible tested host GPU; mobile full-duplex support is unverified.
- **Wispr Flow:** a separate, cloud-dependent Android dictation app. Not an offline downloadable model and not a z0live agent runtime.

## Hardware eligibility and privacy
The Galaxy S25 Ultra manual preset reflects published 12 GB / Snapdragon 8 Elite specs, **not** a measured current device. `Inspect this browser` is user-triggered and retrieves only broad user-agent, core/memory hints and optional WebGL renderer. It does not claim to detect NPU driver support, RAM actually available to a model, usable VRAM or infer throughput. No hardware probe is sent to the backend or persisted.

## Next engineering gates
1. **Phone-harness plugin dogfood:** one authorized Wi-Fi ADB device; approval on, telemetry off. Verify read-only screen -> benign action -> verified UI state, with timestamps and no private screenshot uploads.
2. **Selection admission:** z0intelligence combines explicit preference + actual device inventory/benchmarks + privacy/budget constraints into a versioned, admitted VoicePlan. Never map the exported preference directly to authority.
3. **Transport:** secure phone-to-host WebRTC or mutually authenticated WebSocket; no unauthenticated `0.0.0.0:8765` exposure to LAN or Internet.
4. **Local ASR spike:** measure sherpa-onnx Qualcomm QNN vs Android on-device recognizer CPU/NPU/battery/latency. Keep display/recording permissions user opt-in.
5. **Action trust boundary:** typed PhoneActions via z0int/Jev, explicit approvals and verifiable outcomes; retain launcher hidden-app and profile restrictions.

Sources: [Phone Harness](https://github.com/ShawnPana/phone-harness), [sherpa QNN demos](https://k2-fsa.github.io/sherpa/onnx/android/apk-qnn-asr-streaming.html), [Moshi FAQ](https://github.com/kyutai-labs/moshi/blob/main/FAQ.md), [Wispr Android](https://wisprflow.ai/android).

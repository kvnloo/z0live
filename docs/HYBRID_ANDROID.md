# Hybrid voice: Android microphone + Tern + Wi-Fi ADB

**Target:** Galaxy S25 Ultra running Tern, OMP/Hermes and a currently authorized `adb` Wi-Fi connection to its own Android OS, with Omalauncher/Lawnchair available as the launcher. Tern and Android Chrome share the phone's loopback network namespace; the first supported connection is **local** `127.0.0.1`, not a PC-hosted LAN WebSocket. NVIDIA PC GPU acceleration is an optional second phase.

## Same-phone steps

Inside Tern in this z0live checkout (branch `feat/android-voice-provider-picker`):

```bash
bash scripts/tern-hybrid.sh check
bash scripts/tern-hybrid.sh prepare
```

`prepare` installs the z0live Python package, builds the media UI, and symlinks the OMP extension. **It does not install models, change the current launcher, pair ADB, turn on microphones or start a network listener.** A working OMP environment and an admitted z0intelligence VoicePlan are required to start the real actor.

In OMP, start a separately admitted voice session:

```text
/brainstorm on
```

Then from a phone browser or HyprSpaces → **Hybrid voice · Tern (local)** open:

```text
http://127.0.0.1:8780/
```

To choose between providers first, open `http://127.0.0.1:8780/voice-setup.html`. Tap **connect** to request microphone access. Disconnect stops local capture. Supported streams are **Ogg/Opus 24 kHz** (PersonaPlex/Moshi) and **PCM16 24 kHz** (OpenAI Realtime/future models). The optional separate control-transcriber lane remains PCM16/16k. A manual text command box can submit an explicit user-initiated request to the existing OMP bridge; command acceptance is **not** proof that a phone action ran.

## Phone actions

Hermes integration from [ShawnPana/phone-harness](https://github.com/ShawnPana/phone-harness/tree/main/integrations/hermes):

```bash
export PHONE_HARNESS_PLATFORM=android
hermes plugins install ShawnPana/phone-harness/integrations/hermes
```

Enable the plugin when asked and open a **new** Hermes session. The skill grants the agent a `phone_exec` tool; keep **each-script approvals** enabled and leave `PHONE_HARNESS_CLASSIFIER_AUTO_RUN` unset. Approve only intended actions. Phone-harness executes Python within Hermes's host permissions, so do not expose it to untrusted messaging sessions. z0live does not execute raw adb commands or silently delegate phone control.

**Important:** the OMP `/brainstorm` extension hands commands to **OMP**. This does not automatically expose Hermes's `phone_exec` inside OMP or create an OMP→Hermes agent-call bridge. To use phone-harness for actions, talk to Hermes with the installed plugin or implement and approve a separate typed OMP→Hermes bridge. Do not treat a successful OMP receipt as device proof.

## Security / limitations

- Gateway explicitly refuses non-loopback bind addresses and cross-origin browser sockets; local OMP WebSockets without an Origin header are allowed. No remote microphone streaming or unauthenticated LAN control.
- The browser explicitly refuses non-loopback gateway URLs.
- No consumer ChatGPT login is reused as a Realtime API credential.
- The voice UI is **not** the Android OS default assistant, a wake-word service, or a background listener; Chrome microphone foreground/background behavior is device/browser dependent.
- The server must have an admitted VoicePlan and an actual voice actor. Realtime API usage may incur developer charges. Mobile GPU/NPU acceleration is not shipped by this branch and still needs SoC-specific model profiling.
- Physical Galaxy S25 Ultra verification remains required: mic permission, browser audio focus, latency, battery, screen-off behavior, ADB action approvals, and confirmed post-action UI state.

## Qualification

```bash
python3 -m pytest -q tests/test_gateway.py tests/test_hybrid_phone.py
cd web && npm run build
```

First dogfood: (1) keep paired ADB untouched, (2) launch voice via HyprSpaces, (3) record p50/p95 speech-end to accepted OMP command, (4) separately approve a harmless Hermes phone action, (5) verify foreground outcome on phone. No screenshots/telemetry upload by default.

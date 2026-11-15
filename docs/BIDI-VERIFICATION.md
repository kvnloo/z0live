# Frameless Bidi acoustic A/B (desktop, headless)

Measured 2026-10-20 on the desktop. Broker `feat/frameless-bidi` (head recorded in the PR), isolated
loopback broker (`desktop/tests/live_isolated_broker.py`, private data directory), real OpenAI
models, real Hermes `clean` work through the native API host, real Haiku liaison routing.

## Method

- **Speech in:** five fixed utterances synthesized locally with Piper (`en_US-lessac-medium`),
  peak-normalized to -3 dBFS, 48 kHz (`desktop/tests/make_test_speech.sh`). Each is played through
  WebAudio into the WebRTC microphone track of headless Chromium. No physical microphone.
- **Audio out:** the model's real WebRTC audio, measured with an RMS level detector (onset
  `> 0.01`, silence after 300 ms below it; the reported stop time is the first silent frame).
- **Same script, both transports** (`desktop/tests/live_audio_turns.mjs`): greeting, simple
  question, "Open Spotify." (the harness acts as the phone: it executes the action and reports
  `tool_result`), "check which git branch the z0live repo is on" (real Hermes terminal work), and a
  long story interrupted after ~3.5 s by "Wait, stop. What is two plus two?".
- **gpt-realtime** runs the installed APK's policy: semantic VAD with `interrupt_response:false,
  create_response:false`, replies only after a real-words final transcript, cancel +
  `output_audio_buffer.clear` on the first real transcription words, broker tools via
  `/v1/tools`, work results delivered by inbox (3 s poll) as a system item + `response.create`.
- **Frameless Bidi** runs the new path: broker signaling and sideband, native delegation,
  OMP's echo gate on the mic, and the words-gated client duck.
- Values are end of the user's speech to the first audible model output unless stated.
  p95 with n≤10 is the maximum or near it; treat it as a tail indicator, not a distribution claim.

## Results (p50 / p95, n runs)

| Measure (ms) | Frameless Bidi (new) | gpt-realtime (current APK path) |
|---|---:|---:|
| Greeting → first audio | **1,095** / 5,611 (8) | 1,917 / 2,421 (10) |
| Simple question → first audio | **960** / 1,166 (8) | 2,756 / 5,356 (10) |
| "Open Spotify" → first audio | **1,624** / 2,168 (8) | 2,215 / 4,140 (10) |
| "Open Spotify" → phone action executed | 2,914 / 6,007 (8) | **1,654** / 3,501 (10) |
| "Open Spotify" → confirmed-result audio | 4,035 / 6,950 (8) | **2,215** / 4,140 (9) |
| Computer work → first audio (acknowledgement) | **1,649** / 1,992 (8) | 1,854 / 2,366 (10) |
| Computer work → real answer audio | **7,398** / 17,939 (8) | 13,950 / 20,980 (10) |
| Barge-in: interruption start → output silent | 1,435 / 1,783 (8) | **329** / 407 (10) |
| Barge-in: end of interruption → reply audio | **1,293** / 1,831 (8) | 2,225 / 3,361 (10) |
| Correct (question / phone / work) | 8 / 8 / 8 | 10 / 10 / 10 |
| Barge answered correctly ("four") | 8 / 8 | 10 / 10 |
| Filler turns ("one moment", "still checking") | **0** / 60 turns | 1 / 70 turns |
| Plumbing / capability-confusion turns ("Hermes", "I can't check") | **0** / 60 | 10 / 70 |
| Mean words per assistant turn | **4** | 16 |
| Provider errors | 0 | 0 |

Typical spoken output, Bidi: "Checking now." … "It's on feat/frameless-bidi." and "On it. Spotify's
open." gpt-realtime: "Checking with Hermes, one moment." … "The z0live repo is currently on the
branch called feat/frameless-bidi at commit …" and "I can't directly check your computer from here,
but we can involve Hermes…".

## What the numbers say

- Conversation is faster and quieter with Frameless Bidi: first audio is about 1 s for greetings
  and questions (vs 1.9–2.8 s), answers are a quarter of the length, and plumbing talk is gone.
- Real work answers arrive about twice as fast (one persistent Hermes session, no liaison/meta
  hop, no inbox polling). Work itself is real: every run ran `git` in the repository through Hermes.
- **Phone actions are slower** with Bidi (confirmed result ~4.0 s vs ~2.2 s): the model has no
  function tools, so a liaison call (~0.7 s) picks the tool and the phone picks it up from the inbox.
  2 of 8 runs needed missed-delegation recovery (+1.5 s grace).
- **Barge-in stop is slower** with Bidi (~1.4 s vs ~0.33 s). The Bidi model only emits the
  interrupting transcript when it yields, so a words-gated client cannot stop sooner; the old path
  cancels on the first streamed transcription words. The reply after the barge is faster.
- One greeting outlier per batch (5.6 s here; 16 s in an earlier batch) is the model holding its
  first turn; it is in the p95.

## Earlier batches and robustness findings

- Before the persona v2 / recovery fixes, 2 of 6 and later 1 of 6 phone requests were answered
  "On it. Spotify's open." **without any delegation**. Persona v2 forbids unconfirmed action claims;
  the broker now runs a turn that was only acknowledged. After the fix: 14/14 and 8/8 executed.
- One call died when the sideband keepalive ping timed out under audio load; fixed (deep receive
  queue, no client pings, side effects off the reader thread, up to 3 reattaches). 0 drops after.
- Echo: with the model's output mixed back into the mic at 0.5 gain, 3 runs with the OMP echo gate
  and 3 without both completed greetings and barge-ins without self-interruption; the gate blocked
  ~35% of mic frames that carried signal during output.

## Not verified

- No physical microphone, speaker, Bluetooth, Android device or Samsung audio path. The Android
  change compiles and passes JVM protocol/barge tests; it is not handset-verified.
- OMP `/live` itself was not run head-to-head (it needs a live audio device in its TUI). The new
  path uses the same model, signaling, sideband and delegation protocol as OMP `/live` at
  origin/main, so its conversational behavior should match; the broker adds phone routing,
  recovery and transcript history on top.
- Raw per-run outputs stay private in the active Hermes profile scratch directory.

## Hybrid local provider evidence boundary

The optional `desktop-hybrid-http` provider is not part of the Bidi measurements above.
OpenAI remains the default provider. Its existing Bidi/Realtime negotiation is unchanged.
The local path accepts client-finalized transcripts, uses llama.cpp for a bounded acknowledgement,
and obtains PCM from Pocket TTS. Final requests enter the existing pinned L1–L3 stack.
Clients must supply capture, ASR, AEC, playback and immediate epoch-based interruption.

The new offline fake-transport tests were not executed during implementation.
They do not establish real local inference, acoustic TTFA, physical barge silence or handset parity.
Pocket WAV buffering also prevents treating adapter return time as streaming first-audio latency.
Run the offline commands in [STACK-VERIFICATION.md](STACK-VERIFICATION.md#hybrid-local-l0-conformance-not-executed-in-this-change)
after integration. Keep any future local-provider measurements separate from these historical
OpenAI receipts and from owner-authorized physical microphone/speaker acceptance.

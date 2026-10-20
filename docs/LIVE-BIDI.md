# Frameless Bidi live calls (OMP `/live` architecture)

z0live's phone voice now runs the same architecture as OMP `/live`: OpenAI's Codex
**Frameless Bidi** model `gpt-live-1-codex`, with native **client delegation** into Hermes.
The previous `gpt-realtime` + function-tools path stays available as a fallback.

## Why

The owner's recorded sessions showed the gpt-realtime path's failure modes: it announces
plumbing ("let me ask Hermes"), denies abilities it has, narrates waiting, and gives long
answers. Its ~10 function tools plus an inbox make the voice a router that talks about routing.
OMP `/live` avoids this by construction: the voice model and the coding agent are **one
assistant**. The model delegates natively; progress comes back as silent commentary and the
final answer as context the voice presents as its own.

## Architecture

```text
phone mic/speaker ⇄ WebRTC media (direct) ⇄ OpenAI gpt-live-1-codex
      │ SDP offer (device bearer)                     ▲ sideband WebSocket
      ▼                                               │ (Codex OAuth, broker-owned)
desktop broker  ── POST /codex/realtime/calls ────────┘
      │  delegation.created ─► route
      │     ├─ one declared phone tool ─► inbox action ─► phone executes ─► tool_result
      │     └─ anything else ─► Hermes clean, persistent session `z0live-voice` (POST /v1/runs,
      │                          steer while running, stop on "never mind")
      └─ delegation.context.append: progress → channel "commentary" (silent)
                                    final    → "Agent Final Message": … (spoken as its own)
```

- **Signaling** (`live.py`): the phone posts its SDP offer to `POST /v1/live/calls`. The broker
  sends `{sdp, session:{model, instructions, audio.output.voice, delegation:{type:"client"}}}` to
  `https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas` with
  the host's ChatGPT/Codex OAuth (`~/.codex/auth.json`, never returned or logged) and the Codex
  Desktop headers (`OpenAI-Alpha: quicksilver=v2`, originator, version, account id). The call id
  comes from the `Location` header. Wire details are in `bidi_protocol.py`, mirrored from OMP
  `packages/coding-agent/src/live/{protocol,transport}.ts`.
- **Sideband**: the broker opens `wss://api.openai.com/v1/live/{call_id}` with the same headers,
  retrying 5× with backoff like OMP. Base64 audio frames that also flow there are skipped by a
  prefix check before JSON parsing. User and assistant `turn.done` transcripts are recorded as
  normal session events, so Hermes summaries and history keep working.
- **Delegations** (`delegation.py`): one persistent Hermes session (`z0live-voice`, profile
  `clean`, through the existing native API host on `127.0.0.1:8644`). A new request while a run is
  active **steers** that run (`POST /v1/runs/{id}/steer`) and the newest delegation receives the
  final answer, as OMP does. `message.interim` text and `tool.started` lines become `commentary`
  chunks (≤500 UTF-8 bytes each, ≤24 per run). The final run output becomes one
  `"Agent Final Message"`. A user turn that is exactly "never mind / cancel / stop" stops the run
  and its result is never spoken. Runs carry ephemeral backend instructions: the final reply is
  spoken, 1–3 plain sentences, no plumbing, honest failure, confirmation before destructive work.
- **Missed-delegation recovery**: in real calls the model sometimes answered "On it." without
  creating a delegation. When an assistant turn is only an acknowledgement and no delegation exists
  for that user turn (matched by the provider's `user_bidi_turn_id`), the broker waits 1.5 s and
  then runs the owner's transcript itself; the result goes back through `session.context.append`.
  Unreadable or empty delegation events are recorded in the call stats instead of dropped.
- **Persona** (`live.persona`, version `z0live.bidi.v2`): one assistant, action-biased, brief,
  silent while waiting, never claims an action before its final message, never reads commentary,
  lists the real phone tools from the mint, and says "not wired up yet" for anything else.

## Decision: phone actions are delegation results, not client tools

Frameless Bidi has **no client function tools**. Measured: adding `tools` to the session payload
is accepted at signaling but the call immediately fails with
`Invalid AVAS session_data: [tools] [unknown_parameter] Unknown parameter: 'tools'`. OMP's protocol
has no tool surface either. So phone actions go through delegation:

1. A cheap keyword filter skips anything that is clearly computer work.
2. Otherwise the existing tools-free liaison (`smol` role from the pinned VoicePlan, Haiku 4.5 on
   `127.0.0.1:8645`) picks **one declared phone tool** with arguments, or declines (~0.7 s).
3. The broker queues it as an ordinary inbox action (`say:""`). The installed phone code already
   executes inbox actions and reports `tool_result` with `data.id`; that result (success, failure
   or "not confirmed in 10 s") becomes the final message.
4. Anything declined goes to Hermes, which can still queue phone actions with `bin/z0live action`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `Z0LIVE_LIVE_TRANSPORT` | `bidi` | `realtime` forces gpt-realtime for every client. Clients that do not send `transports` (all APKs before this change) always get gpt-realtime. |
| `Z0LIVE_LIVE_VOICE` | `sol` | Frameless Bidi voice: arbor, breeze, cove, ember, juniper, maple, sol, spruce, vale. |
| `Z0LIVE_DELEGATE_SESSION` | `z0live-voice` | Persistent Hermes session for delegations. |
| `Z0LIVE_DELEGATE_API` | `Z0LIVE_HERMES_API` or `http://127.0.0.1:8644` | Native Hermes API host. |
| `Z0LIVE_DELEGATE_MODEL` / `_PROVIDER` | `claude-opus-5-5` / `anthropic` | Model lock for the delegation session. |
| `Z0LIVE_HERMES_NOTES` | `on` | `off` stops summaries to Hermes (isolated qualification brokers only). |

The sideband needs the `websockets` package for `/usr/bin/python3` (present in the user site);
the rest of the broker stays stdlib-only and unit tests never import it.

## Echo and barge-in

- The model does its own full-duplex turn-taking; the client sends it nothing.
- **Desktop clients** use OMP's echo gate: while output is audible (`> 0.015` RMS), a mic frame
  passes only if its level is at least `max(0.04, 0.65 × output)`. The qualification harness runs
  it in an audio processor before the WebRTC track.
- **Android** relies on platform AEC on the `VOICE_COMMUNICATION` source and adds
  `Z0LiveBidiBarge`: when the live owner transcript shows real words (`Z0LiveSpeechGate.isSpeech`)
  while an assistant turn is open, playback is muted on the phone at once and restored for the
  model's next turn. Noise and grunts never duck. The harness runs the same rule.
- Measured limit: while the assistant is speaking, Frameless Bidi only emits the owner's
  transcript at the moment it decides to yield (about 1.3–1.6 s after the interruption starts),
  so a words-gated client duck cannot stop playback earlier than the model does. Stopping sooner
  would mean ducking on raw voice energy, which the owner explicitly does not want (noise must
  never interrupt). OMP `/live` has no client barge at all, so this is parity with OMP and slower
  than the old gpt-realtime path, which cancels on the first streamed transcription words.

## Verification

See [BIDI-VERIFICATION.md](BIDI-VERIFICATION.md) for measured results. Unit tests:
`python3 -m unittest desktop/tests/test_bidi.py` (wire format, signaling, transport choice,
sideband reading and retry, ownership, end/close, Hermes commentary/final, steering, cancel
silence, honest failure, phone route + inbox round trip, router validation).

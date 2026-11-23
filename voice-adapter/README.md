# Hermes mobile subscription voice adapter

Local deployment for the official Hermes Android beta's built-in Live Voice mode.

## What runs

- `hermes-mobile-subscription-voice.service` listens on `127.0.0.1:9168`.
- The existing `hermes-drive-web-proxy.service` routes only the exact
  `/api/audio/voice-live/{status,session,diagnostics}` endpoints to it.
- Other dashboard and chat traffic is unchanged. The Hermes dashboard/gateway is not restarted.
- Both services are user services and persist across login/reboot under the owner's existing service setup.

## Credentials and trust

The existing Hermes dashboard validates every native bearer via `/api/auth/me`.
Missing, invalid and cookie-only authentication is rejected before voice sessions can be created.
The adapter uses the host Codex CLI's ChatGPT subscription login, not `OPENAI_API_KEY`.
When expired, it asks the installed Codex app-server's `account/read` API to refresh
its own managed login. It never hand-writes account tokens or falls back to API billing.
No account credentials are returned to the phone. Audio and transcripts are never
persisted; diagnostics contain peer state, event-type counters and frame counts only.

## Media and protocol

The mobile client uses `session.delegation.created`, `session.*_transcript.delta`,
`session.thinking.append` and `session.commentary.append`. Codex subscription voice
uses `delegation.created`, `input_transcript.added`, `output_transcript.added`,
`turn.done`, and `delegation.context.append` / `session.context.append`.

Two WebRTC peers on the owner's host bridge audio and translate these data-channel
messages. Audio therefore goes phone -> owner's host -> OpenAI, rather than directly
phone -> OpenAI. Delegated work still belongs to the phone's existing Hermes chat.
Quiet progress and speakable final results remain distinct. Unknown/foreign delegation
IDs are rejected, transcript finals are deduplicated, and app mute silences input frames.
The subscription voice is `gpt-live-1-codex`, voice `sol`.

Calls close with the app/provider/transport, after an unsuccessful 35-second connection
setup, or at a 30-minute safety limit. At most two simultaneous calls are allowed.

## Host delegation (Android 1.0.0 workaround)

Hermes Android 1.0.0 (versionCode 21) compiles `Regex("(?U)\\s+")` while formatting the voice
transcript for a `session.delegation.created` event. Android's ICU regex engine rejects `(?U)`,
so each delegation stopped the call with "Syntax error in regexp pattern near index 3".
The adapter now keeps delegation events away from the phone. It runs the delegated request
on the host with `hermes [-p <profile>] chat -q <request> --oneshot -Q`, using the profile from
`X-Hermes-Profile`/`?profile` (existing profiles only, else default), and returns the answer
to the voice model as the Agent Final Message. Failures are spoken. Each run resumes the profile's
last voice session (`--resume`) while it was used within 30 minutes
(`HERMES_MOBILE_VOICE_SESSION_IDLE`), so follow-ups like "yes, do it" keep their context;
turns per profile run one at a time, and a failed resume falls back to a fresh session.
They are still not the phone's open chat. To apply code changes without cutting a live call,
run `systemd-run --user ./restart_when_idle.sh`. Set
`HERMES_MOBILE_VOICE_CLIENT_DELEGATION=1` to restore client delegation once the app is fixed.

## Verification

```sh
.venv/bin/python -m unittest -v test_mobile_live test_routing
.venv/bin/python live_speech_smoke.py
```

The deterministic suite includes a real localhost two-peer WebRTC fixture, bidirectional
audio frames, data-channel delegation/results, mute/close, auth rejection, and exact
proxy routing. These fixtures are not claims about external provider behavior.

`live-speech-smoke-receipt.json` records a separate actual OpenAI subscription session
using an offline-synthesized generic spoken utterance, not the owner's microphone.
Both peers connected; the provider transcribed the utterance, emitted a client delegation,
accepted a scripted connection-fixture result, and returned the expected spoken answer
with non-silent received audio frames. The scripted result is not a Hermes tool run.
The earlier context-only `live_smoke.py` can connect but does not reliably elicit speech
when no user utterance has occurred; use the acoustic test above as the repeatable oracle.
The actual handset microphone, audible reply, delegation into a real chat, and acoustic
interruption still require the owner to exercise Live Voice. Do not report those as
verified merely from the smoke receipt.

## Operations

```sh
systemctl --user status hermes-mobile-subscription-voice.service
journalctl --user -u hermes-mobile-subscription-voice.service --since '5 minutes ago'
```

Deployment unit: `~/.config/systemd/user/hermes-mobile-subscription-voice.service`.
Proxy activation: `~/.config/systemd/user/hermes-drive-web-proxy.service.d/mobile-subscription-voice.conf`.
Proxy source: `dashboard_proxy.py` in this folder (vendored from the desktop host's chief-of-staff `tailnet_dashboard_proxy.py`; `HERMES_DASHBOARD_UPSTREAM` and `HERMES_PROXY_PORT` set the dashboard address and listen port).

To roll back voice routing without changing Hermes, remove only the named proxy
activation drop-in, daemon-reload, and restart only the proxy. Then disable/stop the
adapter service. Keep the earlier WebSocket subprotocol fix in the proxy.

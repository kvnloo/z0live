# z0live requirements (owner asks, newest first)

North star (Kevin, 11-19): **Dessa-level conversation with Hermes-level intelligence.** Dessa is the UX bar
(natural, expressive, continuously attentive full duplex; `docs/DESSA-PHONE-NORTH-STAR.md` on PR #48), and
Hermes is the brain (its tools, memory, sessions and approvals). The voice model talks; Hermes works.

Bar to beat today (11-16): "Samsung speech-to-text + Enter + reading the reply is a better experience than
live conversation." Every requirement below serves beating that.

Sources: owner messages in Hermes `clean`, `ayo`, `poteto` state.db, mined 11-19 (ledger method:
`owner-history-failure-ledger` skill). Dates are 2026 CST.

## Now (11-19)

| # | Requirement | Source |
|---|---|---|
| N1 | GPT-Live stays the **default** engine (already supported); Grok Voice selectable to test, because it leads on task completion and reasoning | 11-19 |
| N2 | z0live becomes an **official Hermes plugin** (catalog entry) | 11-19 |
| N3 | **Continuous hill-climb** on z0live using every device (Studio, the desktop host, MBP, S25, S22, tablet) | 11-19 |
| N4 | Change z0live config **before or during a call**: lanes, and the voice model (GPT-Live, STT+TTS chain, Claude, Grok) | 11-19 |
| N5 | Runs on the Mac Studio, durable and documented in the home repo; k3s control plane on the Studio | 11-19 |
| N6 | Claude voice: Claude as the brain behind STT/TTS (Anthropic has no voice API); Claude-app connector path is a separate decision | 11-19 |

## Data privacy rule (owner, 11-20)

- **Voice-call traces** (what Kevin said on z0live calls) already went to the providers on that call: OpenAI via
  GPT-Live, and Anthropic and OpenAI via the lanes. Sending that content to those same providers again (evals, lane
  tests, graders) is OK.
- **Private data** (Telegram, Hermes chat history, owner-msgs, anything never sent on a call) stays local: local
  models only, for inference, grading and training. The personal intent model is trained locally.
- When unsure which kind a piece of data is, treat it as private.

## Call experience (11-15 .. 11-16)

| # | Requirement | Source |
|---|---|---|
| C1 | The call knows the chat's context from second one: no catch-up on old messages, no amnesia, knows standing approvals (adb/screen) | 11-16 16:08 |
| C2 | A context packet is prepared before the call and handed to the voice session | 11-16 16:08 |
| C3 | The call survives leaving the Hermes call screen: switch to z0live; z0bridge runs on the phone | 11-16 14:11 |
| C4 | Thinking-orbs bubble on the phone; talk to it to control the phone and Hermes | 11-16 12:33; 11-02 |
| C5 | z0live is the single conductor: lane count, lane responses, merging, routing to local models | 11-15 15:23 |
| C6 | Fast lanes are actually fast; lanes dynamic ("5 as a test"; "2 fast, 2 large, 1 council" by voice or text) | 11-15; 11-05 |
| C7 | Voice turns land in the originating Hermes chat; continuity after hang-up | 11-15 08:49 |
| C8 | Concise, top-loaded replies; voice acts as chief of staff across all sessions | 11-16 12:33 |
| C9 | Works across the OS (Lawnchair, Detent); seamless switch from Hermes live mode to the home screen | 11-15; 11-05 |
| C10 | Other apps (taste skill, music, fframestudio) connect through ACP / t3code / z0bridge, simplest path | 11-16 12:33 |
| C11 | Never a cold start; warm when the call opens | 11-10 23:09 |

## Method (11-10 .. 11-15)

| # | Requirement | Source |
|---|---|---|
| M1 | Hill-climb with a rubric, a feedback system and agreed principles; design an experiment that runs without Kevin | 11-15 18:53 |
| M2 | Account for rate limits; prefer free models when smart; optimize TTFT, tokens/s, parallel lanes | 11-15 18:53 |
| M3 | Per-call event traces so one failed call can be debugged (done: `HERMES_MOBILE_VOICE_TRACE_DIR`) | 11-10 |
| M4 | Verify on a real call or real provider before saying "fixed" (unit tests are not proof) | 11-16 16:44 |
| M5 | Upstream the lanes architecture where it helps others (hermes-talk, RFC #77111) | 11-15 15:27 |
| M6 | On-device voice models on the S25 via autokernel (Dessa doc slices B-E) | 11-05 |

## Dessa test contract (from DESSA-PHONE-NORTH-STAR.md, pre-registered 10-26)

EOS -> first audible meaningful response p50 <= 500 ms, p95 <= 1,000 ms. Valid barge-in -> acoustic silence
p95 <= 150 ms. No unacknowledged user turn, no stale replay, no duplicate reply. Ten scenarios: quick
question, long thought + pause, backchannel mid-speech, laugh/intonation, mid-agent correction + barge,
double-talk + echo, background TV/music, Bluetooth switch, cancelled Hermes work + late result, 30-minute
session. Replay, permission-boundary, cancellation or self-interruption regressions block any candidate.

## Official Hermes plugin: what the catalog requires (plugin-catalog/README.md, read 11-19)

- One `plugin-catalog/z0live.yaml` PR to NousResearch/hermes-agent, full 40-char SHA pin, owner submission,
  `hermes plugins validate` green, capabilities that match what the plugin registers.
- **Rule 9, no core override:** a plugin may not take over core routes. Today the Hermes app reaches z0live
  only because a proxy intercepts the core `/api/audio/voice-live/*` paths. That shape cannot be listed.
- **Rule 11, credentials through Hermes:** a Codex/ChatGPT sign-in only via `credentialed_provider_request`
  with `requires_auth: [openai-codex]`; never read `~/.codex` (the adapter does today). xAI OAuth is not yet
  in that helper's provider table (only `openai-codex`), so Grok needs an upstream addition or `XAI_API_KEY`.
- **No duplex-voice seam exists in core.** `TTSProvider` and `TranscriptionProvider` have register APIs;
  audio-to-audio sessions do not. RFC #77111 (open, 29 comments, Kevin commented 09-15) proposes
  `RealtimeVoiceProvider`. The official-plugin path is: land that seam (or the voice-live route delegating
  to a registered provider), then z0live registers GPT-Live (subscription), Grok and the lanes conductor
  as providers through it.

## Known gaps (measured 11-19)

- the desktop host k3s node NotReady since 11-17; MBP control plane disk 96%; semantic router scaled to 0.
- Local lane models (the desktop host llama-servers on 11530/15637) are down, so the core's local fast lanes answer nothing.
- S25 wireless-debugging port changes on every restart; Studio adb reaches it over the tailnet once told the port.
- The phone app must point at the Studio proxy `:9165`; calls to `:9119` reach stock Hermes voice, not z0live.

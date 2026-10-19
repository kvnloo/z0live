# z0live

Portable realtime conversational voice runtime and fixtures for Zer0.

## Existing portable runtime work

The architecture in [RFC #1](https://github.com/kvnloo/z0live/issues/1) remains the project boundary:
`z0intelligence → VoicePlan → z0live ConversationActor → harness-native tools`.
z0intelligence owns model selection; z0live owns actor lifecycle, floor/barge behavior, replay and voice
fixtures; Hermes/OMP own execution and approvals. The substantial portable v0 implementation is in
[PR #6](https://github.com/kvnloo/z0live/pull/6), with Android/Tern/local voice additions in
[PR #7](https://github.com/kvnloo/z0live/pull/7). Neither branch is replaced or merged by the desktop broker.
The frozen certification wrapper is [z0evals PR #86](https://github.com/kvnloo/z0evals/pull/86).

## Desktop phone broker (this component)

`desktop/z0live_desktop/` is a **separate host deployment adapter**, not a replacement for the portable
`src/z0live` actor package or its v0 WebSocket protocol. It adds the owner's explicitly selected ChatGPT
Realtime + Hermes route without model-ranking policy:

```text
Android / web ── HTTPS tailnet /v1 ── z0live desktop broker
     │                                     │
     └──── direct WebRTC ── OpenAI          └── durable native runs ── Hermes (z0live)
                 audio                                          claude-opus-5-5
```

- Python stdlib only for the broker; OpenAI ephemeral secrets from existing host ChatGPT OAuth.
- Session-scoped schemas/persona; phone executes declared local tools and builtin dispatch.
- Authenticated events, history, inbox/SSE; per-session JSONL plus durable SQLite queues/cursors.
- Immediate owner feedback + compact active/end/idle summaries into one persistent Hermes session.
- Dedicated native Hermes API-only host: no messaging adapters, cron or kanban startup.
- Device pairing/revocation; 0600 verifier/key/database files under a 0700 data directory.

**API/Android integration:** [docs/API.md](docs/API.md).
**Deployment, recovery and verification:** [docs/OPERATIONS.md](docs/OPERATIONS.md).

```bash
# Host CLI (does not overwrite the future portable Python package)
./bin/z0live pair phone-name
./bin/z0live devices
./bin/z0live serve

# Deterministic tests; no provider credentials/network needed
python3 -m unittest discover -s desktop/tests -v

# Opt-in real OpenAI WebRTC + native Hermes test; requires installed/running host services
npm ci --prefix desktop/tests
node desktop/tests/live_webrtc.mjs
```

The deployment intentionally leaves Garden Room unchanged; API.md describes a future adapter.
Live tests prove transport, GPT tool dispatch, native Hermes processing and reply delivery, **not**
physical Android microphone capture, speaker routing, background service policy or local phone actions.

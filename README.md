# z0live

Portable realtime conversational voice runtime for Zer0.

```text
hardware / policy
      |
      v
z0intelligence -- VoicePlan --> z0live -- local gateway --> OMP/Hermes plugin
                                  |
                                  +-- PersonaPlex
                                  +-- OpenAI Realtime (optional)
                                  +-- deterministic fake actor
```

## Ownership

- **z0intelligence** selects/admit actors and emits `z0int.voice_plan.v1`.
- **z0live** owns actor lifecycle, floor/barge behavior, harness attention, replay, receipts, and provider adapters.
- **OMP/Hermes plugins** own harness-native execution bindings and UI.
- **z0evals** may pin exact fixture revisions; fixtures remain canonical here.

There is no model-ranking policy in this repository.

## Run

```bash
pip install -e .
z0live smoke
z0live replay fixtures/core-v1.json
z0live replay fixtures/stress-v1.json
z0live speculation-replay fixtures/speculation-v1.json
```

For local PersonaPlex:

```bash
export HF_TOKEN=...
bash scripts/setup-personaplex-nf4.sh
z0live serve --plan /path/to/voice-plan.json
```

PersonaPlex is session-scoped and never starts on import or normal harness startup.

## Plugin gateway

Default: `ws://127.0.0.1:8765`.

On connect, z0live sends `z0live.gateway.v1` `hello` with actor/harness capabilities.

Client → z0live:

- binary frame: one actor-native input-audio chunk
- `event`: VAD/floor event such as `user.speech.started`
- `harness.command`: submit/steer/redirect/cancel/approve
- `interrupt`, `ping`, `close`

z0live → client:

- binary frame: actor-native output-audio chunk
- `event`: canonical timeline event

The hello payload declares codec/sample rate. PersonaPlex currently uses Opus/24k; OpenAI Realtime uses PCM16/24k.

## Harness bridge

`--harness HOST:PORT` attaches the generic NDJSON bridge. OMP/Hermes plugins expose this small contract; z0live does not import harness internals.

```text
harness -> hello(capabilities)
z0live  -> command
harness -> result(command_id)
harness -> event(TimelineEvent)
```

Harness commands run in background tasks and never block the audio loop.

## Fixtures

```bash
z0live replay fixtures/core-v1.json
```

`core-v1` contains 20 deterministic fixtures: five each for floor, barge-in, attention, and agent-result semantics. Replay outputs include the exact fixture SHA-256.

## Actors

### PersonaPlex

Implements NVIDIA PersonaPlex/Moshi binary WebSocket protocol:

- `0x00`: handshake
- `0x01`: Opus audio in/out
- `0x02`: assistant text token out

The current upstream prompt is startup-only, so dynamic quiet context/commentary injection is explicitly unsupported rather than faked.

### OpenAI Realtime

Optional:

```bash
pip install -e '.[openai]'
```

A selected `openai_realtime` VoicePlan uses the current OpenAI Python Realtime SDK. z0live maps audio/VAD/transcript events to the canonical timeline and supports response cancellation/context injection.

## State

`~/.z0live/session.json` exists only while active. Lifecycle + resource receipts append to `~/.z0live/receipts.jsonl`.

## Tests

```bash
pip install -e '.[test]'
pytest -q
```


## Full-duplex browser client

z0live ships a small local browser client using the same Ogg/Opus worker transport as PersonaPlex.

```bash
bash scripts/setup-web.sh
```

`z0live serve` then serves the client at `http://127.0.0.1:8780` when `web/dist` exists. The browser connects to `z0live.gateway.v1` on port 8765, streams microphone Ogg/Opus pages, decodes assistant audio, and emits user/assistant speech boundaries from actual microphone/playback audio for latency receipts.

## OMP plugin

```bash
bash scripts/install-omp-extension.sh
```

Then:

```text
/brainstorm on
/brainstorm status
/brainstorm off
```

OMP remains the execution/authorization authority. The extension forwards events and control; it does not add z0live-specific code to OMP core.

## Speculation

`speculation-v1` contains 180 frozen partial/final transcript cases. Partial speech may return a side-effect-free `prewarm` ticket, but only the final transcript returns `authority`.

Measured prewarm traces are evaluated separately:

```bash
z0live speculation-eval measurements.jsonl
```

The pre-registered promotion gate is at least 50 ms p50 useful gain and exactly zero speculative mutations.

## 3080 Ti dogfood

After installing z0intelligence + z0live and the PersonaPlex runtime:

```bash
bash scripts/dogfood-rtx3080ti.sh
```

It preserves host inventory, exact VoicePlan, 20-case core replay, 120-case stress replay, 180-case speculation replay, smoke result, a headless PersonaPlex warm/handshake probe, before/after VRAM, and SHA-256s in one receipt directory. Headless success is not treated as a naturalness claim.

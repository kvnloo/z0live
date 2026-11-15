# Desktop multi-agent voice architecture

This implements the desktop host-adapter slice of [RFC #1](https://github.com/kvnloo/z0live/issues/1):

```text
z0intelligence → immutable VoicePlan → desktop ConversationActor binding
                                            │
Human ↔ L0 ChatGPT Realtime (direct WebRTC)   │ floor/barge/replay
                  │ ask_z0                  ▼
                  └─────────────── L1 information liaison
                                    │ cached answer / clarification
                                    │ faithful brief + initial State Packet/memory
                                    ▼
                             L2 persistent native Hermes `z0live`
                                    │ decides whether to fan out
                                    │ z0live fanout --turn … --brief … --brief …
                                    ▼
                             L3 native Hermes task-role runs (2 concurrent)
                                    │ cited findings / terminal receipts
                                    └──→ L2 → L1 voice condensation → inbox/SSE
```

Immediate queued acceptance is data-only (`say:"",speech:"silent"`): L0 gives at most one short acknowledgement before dispatch, then waits quietly. Memory lookup, native start and worker admission stay in traces, not speech. Only verified partial findings or the final answer enter the inbox. Normalized partial findings deduplicate per turn across retries/restarts, and the final answer replaces unheard floor-held progress.

The implementation is **not** a replacement for `src/z0live` in unmerged PRs #6/#7 or its WebSocket protocol. `liaison.py` adapts the desktop HTTP broker's session, floor and native-run surfaces to the RFC vocabulary. It is not a second selector or a general agent runtime. Hermes owns credentials, tools, approvals, sessions, run semantics and tool process interruption. Real task/issue tracking stays in Hermes/GitHub, not in the observational turn tables.

## Pinned selection, not routing

The source profile and export are in [z0intelligence PR #174](https://github.com/kvnloo/z0intelligence/pull/174): `manifests/z0live_voice.v1.json`, `python -m z0int.voice_plan PROFILE`. This is an owner-selected deployment profile, **not** a local benchmark promotion. It does not repurpose the unrelated local cognition specialist `role_defaults`.

Install the exported JSON privately at `$Z0INT_HOME/config/z0live-voice-plan.json` (default `~/.z0int/config/`), or set `Z0LIVE_VOICE_PLAN` to an exported file. The broker checks schema, literal identifiers, SHA-256 digest and supported actor adapter, then stores a full copy under the new session ID. Changing the file affects only new sessions. An invalid plan rejects mint; an absent plan leaves legacy tools available but `ask_z0` reports unavailable. There is no implicit provider fallback.

| Layer | Plan role | Selected provider/model |
|---|---|---|
| L0 actor | VoicePlan actor | `openai/gpt-realtime`, voice `marin` |
| L1 liaison | `smol` | `anthropic/claude-haiku-4-5` |
| L2 meta | `meta_role: plan` | `anthropic/claude-opus-5-5` |
| L3 workers | `task` | `openai-codex/gpt-6.1-sol` |
| General role | `default` | `openai-codex/gpt-6.1-sol` |
| Deliberate role | `slow` | `anthropic/claude-opus-5-5` |
| Optional gate | `judge` | `typesafe/jev-latest` (reserved; not invoked) |

`smol`, `default` and `judge` mirror the owner's OMP role vocabulary. The desktop profile deliberately preserves the already authorized Opus Hermes meta session and Sol worker provider instead of assuming OMP's current xai-oauth plan/task bindings work in Hermes. Luna or a different worker requires an explicit z0intelligence profile change and new session; z0live never chooses it turn-by-turn. Changing the meta model also requires explicitly preparing a compatible native session, not bypassing its model lock. No private voice context is sent to Jev.

## Contracts and message flow

1. The client mints a session after the human starts voice. It receives the usual ephemeral fields, additive `voice_plan`, and `builtin_tools`. Audio still goes directly to OpenAI.
2. L0 gives a quick acknowledgement and calls **`ask_z0({question})`**. Local phone tools remain direct. Existing builtin names/schemas remain registered; `ask_hermes` and requested `tell_hermes` work become liaison aliases for planned sessions. Other legacy tools remain compatible.
3. Admission is durable and idempotent per `(session_id, provider call_id)`. Changed arguments with the same call ID fail. A queued turn returns immediately with `turn_id`, short `say`, and a trace URL. The queue admits at most eight outstanding turns.
4. L1 serves the exact model-identity question from the pinned plan without inference. For other requests it supplies recent session context/open-job metadata to a **tools-free streaming** Haiku call, obtaining a brief or necessary human clarification. The broker communicates with a loopback L1 inference adapter on port 8645 inside `hermes_host.py`; credentials stay in the native Hermes runtime. This adapter uses the explicit provider client directly, not its automatic fallback chain. JSON/token fragments and reasoning are never spoken.
5. Before handing off, L1 explicitly queries z0intelligence memory/State Packets/code intelligence through the existing bounded source reader. It includes cited initial evidence and gaps in the meta brief; missing evidence is not a conclusion. The L2 prompt instructs further z0intelligence-first investigation, ctx/native history, code search, `gh`, and terminal as appropriate. A failed L1 brief falls back to the **literal owner request**, not another model.
6. L2 uses the existing durable serialized native-run queue and persistent `z0live` session, including compression continuations. Per-turn operating briefs preserve the cached system prompt. The pinned meta binding is explicit in the native request. The dedicated host uses the native adapter's `confirmed_runtime_lock` path for explicit bindings, disabling its fallback chain without changing the shared Hermes profile. Native run acceptance is not task success.
7. L2 chooses whether cheaper context workers are useful. Hermes currently chooses `delegate_task` models through shared configuration, not per-child arguments. The bounded `z0live fanout` host adapter therefore submits **native `/v1/runs`**, with separate model-locked sessions and the plan's `task` binding. It persists each exact request and idempotency key before submission. Up to four bounded briefs, two concurrently; no second worker inference engine or scheduler. Context workers have read-only operating briefs, but remain native owner-authorized tools, **not a security sandbox**. Native approvals remain authoritative.
8. L3 native IDs, start/finish/timeout/cancel receipts and usage are joined to the originating turn. L2 gets cited findings. It can send a verified partial with `z0live progress --turn ID 'finding'`; native lifecycle events stream to the local trace. L1 shortens the final result to at most 700 speech characters, retaining the full result in `params.result`. Only this short result reaches the phone's existing Realtime voice for interpretation, never platform TTS.

The CLI command in each meta brief includes its exact `Z0LIVE_DATA`, so isolated verification never writes the phone's inbox. Newly created native job idempotency keys are broker-directory namespaced and stored durably; previously persisted requests retain the old key for lost-acceptance reconciliation.

## Floor, interruptions and silence

The client reports authenticated `floor` events with `data.state: user|assistant|quiet`; a user floor with `barge_in:true`, explicit `barge_in`/`cancel`, `topic_changed:true`, `cancel_work`, `/v1/cancel`, or a final literal “never mind”/“cancel that” cancels admitted voice work. See [API.md](API.md) for event mapping.

Cancellation marks the turn first, suppresses stale results, cancels the L1 transport, then invokes native `/stop` on **every recorded meta/child run**. A racing native admission remains `cancelling` until its ID is exposed and its terminal state is read back. A stop request is not a cancellation confirmation. Hermes stops its own tool processes; z0live does not kill unrelated sessions. Provider requests already in flight may consume tokens before their transport unwinds. Barge-in cannot undo a tool effect that already landed.

Speech enters an atomic SQLite outbox while the user or Realtime model owns the floor, and flushes only on `quiet`. Final result/checkpoint and speech enqueue commit together. Clients must still queue TTS at their actual audio boundary: the broker cannot observe direct WebRTC audio independently. A Realtime provider-side `response.cancel` **alone does not cancel desktop work**; the client must forward the barge event or invoke cancellation.

**No unsolicited summaries/backlog:** progress is allowed only for an explicitly admitted request in the latest, non-ended voice session for its paired device, with activity in the last 120 seconds. Session summaries, setup/context notes and broadcasts are not spoken. Status-only sessions do not enqueue a summary. Ended/superseded/stale-session inbox items retain their IDs, cursor and historical data but return empty `say`, null `action`, and `params.silent:true`; cancellation similarly suppresses stale turn speech. They are never replayed aloud in a later session. The client drops any already-local queued speech for the interrupted turn. A new mint is not permission to read old inbox speech.

## Latency budgets and observed limits

| Segment | Target / bound |
|---|---|
| First L0 acknowledgement | target <1.5 s; depends on provider/audio/client |
| Cache/admission path | target <800 ms; no L2 wait |
| Actual L1 inference | target <800 ms; **not yet achieved consistently** with Haiku |
| L1 native provider call | 8 s request timeout, 12 s adapter event timeout |
| z0 memory/code initial lookup | existing bounded reader (1.2 s resident HTTP timeout) |
| Meta answer | async; serialized behind existing native work; no voice blocking |
| Each task worker | 120 s default (`Z0LIVE_WORKER_SECONDS`), then native stop + bounded terminal reconciliation |
| Speech | only while active session and floor quiet; 700 characters |

See [STACK-VERIFICATION.md](STACK-VERIFICATION.md) for actual receipts, not an SLA. Headless text-channel latency is not physical first-spoken latency. The fast acknowledgement/cache path is deliberately independent of slower Haiku briefing and heavy native investigation. Native workers currently inherit the full configured Hermes tool/system context; slimming that overhead is a future optimization, not a claimed latency win.

## Failure/recovery semantics

- **L1 slow/down:** immediate acknowledgement still returns. The saved original request goes to L2 unchanged; errors are traced. If condensation fails, a bounded extractive excerpt of the actual native answer is used, not an invented answer.
- **L2 slow/down/approval:** request remains pending with its durable exact payload/run ID; audio stays free. Native transport retry uses the same idempotency key. Native approval/failure is reported honestly; no provider switch or effect replay. The owner can cancel. Work behind earlier serialized requests can be delayed.
- **Worker timeout/failure:** stop native execution; preserve partial/gap receipts. A lost native acceptance is `unknown`, not failed/cancelled: retain the exact request/key, do not launch a replacement, and require reconciliation against the native ledger. Known-ID stop failures stay `stop_pending` and are polled/stopped again without new inference. L2 must not call failed evidence verified. Cancellation stays pending if the native API cannot confirm it.
- **Broker restart:** in-flight L2 keeps its native run ID; no fresh effectful replay. Completed native output is recovered from the committed job result without another model/tool call. Outbox rows and floor state survive. Recovered work for old sessions stays silent.
- **Lost client/reconnect:** active-session expiry prevents late speech; new session gets a new pinned plan. Results remain in local native history/trace, not an audible backlog. Native work is not silently restarted.
- **Unavailable profile/model/credentials:** fail visibly. This deployment does not invent Groq/Cerebras/DeepSeek availability, start a gateway/scheduler, or copy credentials from another profile.

## Observations and verification

`Z0LIVE_DATA/broker.db` is the canonical observation store. `Z0LIVE_DATA/traces/TURN_ID.jsonl` is a private 0600 replay snapshot. Each event carries layer, role, pinned model, timestamp, measured milliseconds, native IDs/runtime identity, usage where supplied, and `cost_usd`/cost status. The trace omits credential values, tool arguments, raw delta/reasoning, and private source text. Native outputs remain private in the broker/native stores.

```sh
./bin/z0live trace --turn TURN_ID
./bin/z0live trace --session BROKER_SESSION_ID
# Device authenticated, exact-session scoped:
GET /v1/trace?turn_id=TURN_ID
```

L0 usage is client-observed `model_usage` telemetry from `response.done`; L1 usage is provider response usage; L2/L3 usage is native terminal-run accounting, including cache counters. Dollar cost is **unknown** when provider billing/prices are not supplied, not zero and not an invented public-price estimate. Interrupted native runs may omit usage; traces say null. The CLI is available to L2 without another inference call. The test suite uses fakes per layer and a separately opted-in real Chromium/WebRTC test; no fake output is labeled real-provider evidence.

## Explicit hybrid local L0 seam

The optional `desktop-hybrid-http` binding replaces only the L0 front end for a new,
explicitly negotiated local session. It does not replace the portable actor in PRs #6/#7.
The default remains OpenAI, including the existing Bidi/Realtime compatibility path.

```text
client capture/ASR/AEC + epoch playback boundary
    → authenticated final transcript/control events
    → local llama.cpp acknowledgement + Pocket TTS PCM
    → existing Liaison.admit → pinned L1 → native L2/L3 → existing speech outbox/inbox
    → authenticated inbox-ID synthesis → same Pocket voice
```

`Z0LIVE_L0_PROVIDER=local` is an explicit enable flag, not model-selection policy.
z0intelligence must supply the complete signed local plan, including HTTP origins and all
existing native role bindings. The broker never rewrites that plan or selects a fallback.
Mint checks the pinned llama.cpp alias and Pocket readiness. Missing backends fail visibly.
No provider credential store, worker scheduler or task ledger is added.

Partials remain ephemeral. Only final text enters existing durable admission with a stable
session/epoch key. The local model can choose silence or a bounded acknowledgement.
It cannot answer work, assert completion, choose a model or authorize tools.
L1–L3 retain their existing approvals, cancellation and terminal-receipt authority.

The broker rejects late local audio when its epoch, session or quiet floor no longer matches.
Barge/cancel disconnects local HTTP generation and invokes the existing native cancellation path.
The client must stop actual playback immediately and invalidate queued PCM before network acknowledgement.
Final results use the canonical outbox and inbox IDs, not a separate result channel.
Acknowledgements are at most once. Failed or floor-suppressed result synthesis remains retryable.
Generated result PCM is cached under a stable speech ID until a client-reported playback
acknowledgement is stored in the existing event ledger. A lost HTTP response can retrieve the same
PCM without another synthesis or work run; the client deduplicates at its playback boundary.
Generation, HTTP receipt, client-reported playback and human audibility are distinct observations.

The benchmark interface is `LocalHTTPBackend(plan, transport=None)` in
`desktop/z0live_desktop/hybrid.py`: `probe()`, `backchannel(text, Cancellation)` and
`synthesize(text, Cancellation)`. Default transport performs real loopback HTTP.
Injected transports are only offline test fixtures. `Cancellation.cancel()` disconnects
attached sockets. WAV buffering makes this a control/provider seam, not streaming TTFA evidence.
See [API.md](API.md#optional-local-hybrid-l0) for the exact endpoint and client media contracts.

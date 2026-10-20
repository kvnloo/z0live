# z0live desktop API v1

Base URL: **https://0.taila30dc.ts.net:8791** (Tailscale Serve → `127.0.0.1:8791`, tailnet only).
All routes, including health and SSE, require `Authorization: Bearer DEVICE_TOKEN`.
JSON POST bodies require `Content-Type: application/json`. Never put tokens in URL queries except the
one-time pairing deeplink; never log that link. HTTPS uses the normal Tailscale certificate.

## Pairing

On the desktop, run `z0live pair NAME --qr` for a terminal QR, or
`z0live pair NAME --qr-png PATH` for a new **0600** PNG (requires `qrencode`). These
flags can be combined; neither mode prints the plaintext token or link. The QR contains
`z0live://pair?url=https%3A%2F%2F0.taila30dc.ts.net%3A8791&token=...`.
Open **z0live → Scan pairing QR** on Lawnchair, scan, then confirm the backend origin.
QR and deeplink input share validation and confirmation before encrypted storage; scanning
uses Google Play services, not a Lawnchair CAMERA permission. A missing scanner module
shows an error and keeps the deeplink/paste fallback. Pending VIEW links remain single-use
in phone process memory until controls become visible; process death requires reopening.
Without QR flags, `z0live pair NAME` (or `./bin/z0live pair NAME`) prints a new device token
and the pairing link for that fallback. A failed QR render revokes its newly minted token.
The token is shown once; SHA-256 token verifiers are stored in a **0600** `devices.json` in the
0700 service data directory. No plaintext permanent device/provider credential is committed.
Android must register the `z0live://pair` intent, validate HTTPS URL/expected tailnet host, then store
the token in Keystore-backed encrypted storage. Treat pairing links/QRs as bearer credentials.
`z0live devices` lists IDs/names, not tokens; `z0live revoke DEVICE_ID` immediately revokes new requests
(established SSE streams recheck at their next 15-second heartbeat).
Tokens have no automatic expiry; pair again after loss/revocation. Each pairing is a separate device.

## Multi-agent work (additive desktop contract)

See [ARCHITECTURE.md](ARCHITECTURE.md) for ownership and failure semantics. New planned sessions expose `voice_plan` (full immutable z0intelligence binding + digest), `ask_z0` and `cancel_work` in `builtin_tools`. The five required ephemeral fields remain unchanged. A missing plan is explicit (`voice_plan:null`); legacy tools still work, but `ask_z0` reports unavailable. Reconnect to adopt new roles; no turn-by-turn model change.

## Endpoints

### `GET /v1/health`

```json
{"ok":true,"version":"1.0.0"}
```

### `POST /v1/realtime/session`

```json
{
  "client":"lawnchair",
  "context":{"app":"com.example.app","screen":"home"},
  "tools":[{
    "type":"function","name":"open_app","description":"Open an installed app, subject to local permission",
    "parameters":{"type":"object","properties":{"package":{"type":"string"}},"required":["package"],"additionalProperties":false}
  }]
}
```

`client` is exactly `lawnchair|assistant|dash|web`. `context` and `tools` are optional. Tool schemas
use OpenAI Realtime function format (not Chat Completions' nested `function`). Max 32 app tools,
48k characters in tools; unique names; builtins cannot be overridden. Context max 16k characters.
The broker registers these schemas, but **the client executes app tools locally** with its own
permission/approval and argument validation. This broker does not grant Android permissions.
Persona `z0live.persona.v3` also injects the exact mint-time builtin + app tool catalog (names,
descriptions and schemas) into instructions, so unavailable tools are not advertised. It identifies
Realtime `gpt-realtime`/`marin`, direct phone/OpenAI WebRTC, the short-lived tailnet mint broker and
separate persistent Claude Opus Hermes session. Local phone commands stay local unless delegation
is requested. Results, ambiguity and required permissions must be reported honestly; dial/SMS tools
open a composer only, never auto-send. Back/recents require explicit optional accessibility and a
supporting declared tool. App context remains metadata. A declared accessibility tool may return bounded
screen text only after Android enablement and explicit per-session phone consent; the broker grants neither.

Waiting is silent: at most one brief dispatch acknowledgement, no status polling, no repeated
“no update yet” turns. Lifecycle telemetry is not speech. Only new verified findings, real blockers,
completion, or an explicit owner question justify another spoken turn.

Response (the first five fields are the stable required contract; `session_id` is additive):

```json
{"value":"EPHEMERAL_SECRET","expires_at":1790000000,"model":"gpt-realtime","voice":"marin","instructions_version":"z0live.persona.v3","session_id":"SERVER_GENERATED_ID"}
```

Use this `session_id` for all events/tools/end calls. Mint at connection time: the client secret expires
120 seconds after creation (provider's `expires_at` is authoritative); establish the WebRTC connection
promptly. This is not the duration of an established conversation. No underlying ChatGPT OAuth token
is returned. The host reads the owner's existing `~/.codex/auth.json` lazily and derives
`ChatGPT-Account-ID`; OAuth refresh failure is a 502, not a paid-key fallback.

### Frameless Bidi transport (OMP `/live` architecture)

Clients that can use it add `"transports":["bidi","realtime"]` to the session request. When the broker
setting `Z0LIVE_LIVE_TRANSPORT` is `bidi` (the default) the broker answers without a provider call:

```json
{"transport":"bidi","model":"gpt-live-1-codex","voice":"sol","instructions_version":"z0live.bidi.v1","session_id":"SERVER_GENERATED_ID","builtin_tools":[],"voice_plan":null}
```

There is no `value`: the phone never holds a provider credential. Requests without `transports`
(every APK before this change) or with `Z0LIVE_LIVE_TRANSPORT=realtime` get the gpt-realtime mint
below, unchanged except for an additive `"transport":"realtime"`. See [LIVE-BIDI.md](LIVE-BIDI.md).

#### `POST /v1/live/calls`

```json
{"session_id":"SERVER_GENERATED_ID","sdp":"v=0\r\n..."}
```

The broker posts the phone's SDP offer to Codex signaling with the host's ChatGPT/Codex login,
opens the call's sideband WebSocket, and returns `{"sdp":"v=0...","call_id":"rtc_...","model":"gpt-live-1-codex","voice":"sol","instructions_version":"z0live.bidi.v1"}`.
Media stays direct between the phone and OpenAI. Errors: 400 for a non-bidi, ended or already-called
session; 404 for another device's session; 502 when signaling fails (clients then mint
`transports:["realtime"]` and post the same offer to OpenAI). Ending the session closes the call.

The phone's data channel receives Frameless Bidi events (`input_transcript.added`,
`output_transcript.added`, `turn.done`, `delegation.created`, `error`) for captions only. It sends
no control messages. Phone actions arrive as ordinary inbox actions (`say:""`); the phone executes
them and posts the usual `tool_result` event with `data.id` set to the inbox id, which becomes the
spoken result. Transcripts are recorded by the broker from the sideband.

#### `GET /v1/live/trace?session_id=...`

Owner-scoped delegation receipts: route (`phone:TOOL` or `hermes`), status, timing marks
(`created`, `routed`, `run_started`, `first_commentary`, `final`) and the final text, plus call stats.

### Direct OpenAI WebRTC (not proxied through desktop)

1. Acquire microphone from a user gesture with Android's microphone permission; use a foreground
   microphone service/notification for ongoing phone-wide capture. Do not evade background-mic rules.
2. Create `RTCPeerConnection`, attach audio input, attach remote audio to the actual output route.
3. Create data channel `oai-events`; create offer and set local description.
4. POST offer SDP **directly** to `https://api.openai.com/v1/realtime/calls`, headers
   `Authorization: Bearer <value>` and `Content-Type: application/sdp`; apply the returned answer SDP.
5. Handle live data-channel events. Audio goes phone ↔ OpenAI, not through z0live or Hermes.
   A data-only/text-mode connection can use an audio recvonly transceiver and
   `session.update` with `{session:{type:"realtime",output_modalities:["text"]}}`.
6. `conversation.item.input_audio_transcription.completed` → `user_transcript` event.
   `response.output_audio_transcript.done` (or legacy `response.audio_transcript.done`) →
   `assistant_transcript`. Text-mode uses `response.output_text.done`.
7. On `response.function_call_arguments.done`, parse arguments, execute the builtin or declared local
   tool, then send `conversation.item.create` with `{type:"function_call_output",call_id,output:JSON.stringify(result)}`.
   Log `tool_call` and `tool_result`. Only send the continuation `response.create` **after** the previous
   `response.done`; function arguments can complete while a response is still active. A result with
   `speech:"silent"` is data-only: append its function output but **do not create a continuation response**.
   It is an async acceptance/no-news receipt, not a new conversation turn. Other parallel tool results
   that require a response must still retain their pending continuation.
8. Never present an upstream error as success. On connection loss close peer/tracks, flush events,
   end the broker session, and mint a new one. Reconnect with bounded exponential backoff.

Builtins available in every session:

| Name | Arguments | Client dispatch |
|---|---|---|
| `ask_z0` | `{question}` | `POST /v1/tools/ask_z0`; preferred single work entry point |
| `cancel_work` | `{}` | `POST /v1/tools/cancel_work`; cancel current session work tree |
| `tell_hermes` | `{text,kind}`; kind `request|complaint|bug|praise` | `POST /v1/tools/tell_hermes` |
| `get_context` | `{}` | `POST /v1/tools/get_context` |
| `summarize` | `{text}` | `POST /v1/tools/summarize` |
| `work_search` | `{query,scope?,project?}`; scope `recent|all` | `POST /v1/tools/work_search` |
| `ask_hermes` | `{question}` | `POST /v1/tools/ask_hermes` |
| `start_task` | `{title,details,project?,priority?}` | `POST /v1/tools/start_task` |
| `task_status` | `{id?,project?}` | `POST /v1/tools/task_status` |
| `what_changed` | `{since?,project?}`; since `today|yesterday|week` | `POST /v1/tools/what_changed` |

New session responses also include `builtin_tools:["tell_hermes","get_context","summarize","ask_z0","cancel_work"]` (persona v4: the legacy work tools in the table stay executable and reserved but are no longer offered to the model). Route these names to the
broker; execute declared app tools locally. Clients with a static allowlist must dispatch all returned builtins, including `what_changed`, `ask_z0` and `cancel_work`. No new pairing/permission is required; reconnect to mint them.

`ask_z0` requires `{session_id,call_id?,arguments:{question}}`. By default (`Z0LIVE_REALTIME_WORK=delegate`) it runs on the persistent Hermes backend shared with Frameless Bidi and returns `{ok,id,status:"working",say:"",speech:"silent"}`; the final answer arrives as one inbox item with `source:"run:<id>"` and `params.delegation_id`, for the phone's model to answer from in its own words. With `Z0LIVE_REALTIME_WORK=liaison` it returns `{ok,id,turn_id,status,say,speech?,trace?}` immediately; queued acceptance uses `say:"",speech:"silent"` (L0 may acknowledge once before dispatch). Repeated pending acceptance stays silent. Cached model identity is `status:completed,cache:pinned_voice_plan`. Otherwise the brief/meta/workers run asynchronously and short progress/final speech arrives through inbox/SSE, with `params.turn_id`, `params.event`, and the full final `params.result`. Reuse the exact provider `call_id` on retry; changed arguments fail. Latest active user-started session only: old/ended sessions cannot admit new liaison work. For planned sessions `ask_hermes` and `tell_hermes(kind:request)` are compatibility aliases to L1 (immediate async response, not the old 24-second wait). Other legacy tool schemas remain unchanged.

Work-tool bodies may include the provider `call_id`. For `ask_hermes` and `start_task`, retrying the
same session/call ID reuses its durable job; changed arguments with the same ID are rejected.
Lookup responses contain `{id,status,partial,say,items,sources,gaps,local_tasks}`. Each item has
`source,project,date,session,what,status,citation`. Ready evidence is returned within a 2.7-second
lookup wait; slower source completion arrives in inbox with `params.lookup_id` and `params.result`.
`ask_hermes` waits up to 24 seconds (use a client HTTP timeout >25 s), then returns a truthful pending
message and delivers the answer later. A synchronous answer is not duplicated in inbox.
`start_task` immediately returns `{ok:true,id:"voice-N",status:"queued",track:"/v1/tools/task_status",say}`;
Hermes subsequently supplies its actual kanban/issue ID and plan. Request completion is not task
completion. Destructive/costly actions require plan readback and explicit confirmation. See
[VOICE-TOOLS.md](VOICE-TOOLS.md) for sources, limits, setup, offline Telegram delivery and live evidence.

Each helper body: `{"session_id":"...","arguments":{...}}`. `tell_hermes` returns
`{ok:true,id:INTEGER,status:"queued"}`. `get_context` returns `{session_id,client,context}`;
`context` starts from session creation and is updated by `app_state.data`. `summarize` queues a compact
handoff; it does not end the session. The tools are not remotely executed by OpenAI: **clients must dispatch them**.
Logging a `tool_call` event alone does not execute it. Alternatively `tell_hermes` can dispatch to
`/v1/hermes/send` with text/session_id, and a summary can be sent as a `summary` event.

### `POST /v1/events`

Body is a **bare batched array**, not `{events:[...]}`:

```json
[
  {"session_id":"...","ts":1790000000.25,"type":"user_transcript","text":"Please fix this","data":{"event_id":"unique-client-event-id"}},
  {"session_id":"...","ts":1790000001,"type":"app_state","text":"","data":{"app":"com.example.app","screen":"details"}}
]
```

Types: `user_transcript|assistant_transcript|tool_call|tool_result|summary|feedback|app_state|floor|barge_in|cancel|model_usage`.

Floor/cancel events are ordered client observations, not inferred from audio on the broker:

```json
[
  {"session_id":"...","type":"floor","data":{"event_id":"floor-42","state":"user","barge_in":true}},
  {"session_id":"...","type":"floor","data":{"event_id":"floor-43","state":"quiet"}}
]
```

Map Realtime `input_audio_buffer.speech_started` to `state:user`, with `barge_in:true` when interrupting outstanding work; `speech_stopped` can release to `quiet` only if no assistant/TTS is active. `response.created` → `assistant`; `response.done` → `quiet` when the user/TTS is also quiet. Explicit `barge_in` or `cancel` events and `data.topic_changed:true` cancel the session's admitted voice tree. The final literal “never mind”/“cancel that” transcript and `cancel_work` also cancel it. Provider-only `response.cancel` does not reach desktop workers. On local barge-in, discard queued TTS for the interrupted `params.turn_id` before speaking anything else.

Optional usage telemetry: `model_usage.data:{turn_id,response_id,usage,ms?}` from `response.done`. The turn must belong to the supplied session. This is client-observed L0 accounting, not a billing assertion. IDs deduplicate telemetry like other events.

`ts` is client Unix seconds (ISO string accepted); server receipt time is stored separately.
`text` max 16k, `data` object; max 100 events/batch, 32k characters/event, 1 MB request.
An invalid batch is rejected atomically; retry only after correction. Response `{ok:true,accepted:N}`.
Use `data.event_id` for persisted deduplication across offline/retry batches; duplicate IDs within
one session are ignored. Omit it only for at-most-once producers. Preserve final transcripts, not audio.
Events append to `sessions/SESSION_ID.jsonl` under the service data dir. SQLite is the canonical durable
store; crash recovery repairs the append checkpoint and incomplete JSONL tails. No credentials/audio
should appear in context/events. Sessions/events/history are scoped to the paired device.

User transcripts are what Hermes **hears** in periodic/end summaries. Feedback events are queued
immediately, independently of whether Realtime chose `tell_hermes`, and included in the next summary.
A `summary` event supplies GPT's compact summary. For normal telemetry send `tool_call.data` as
`{name,arguments,call_id}` and `tool_result.data` as `{name,call_id,result,success}`; these records are
observability, **not executable instructions**. Post an action's actual result to confirm execution.

### `POST /v1/hermes/send`

```json
{"text":"Please investigate this","session_id":"optional originating broker session"}
```

Response `{ok:true,id:INTEGER,status:"queued"}`. Dedicated long-lived native Hermes session:
**title/id `z0live`**, `anthropic/claude-opus-5-5`, cwd this repository. Runs are serialized and use a durable
exact payload + Idempotency-Key. Compression continuations are followed from native run responses.
Accepted is not completed; run failures/interruption are delivered honestly, never automatically replayed
as fresh tool runs. A job without originating session broadcasts its reply to the owner's paired devices.

### `GET /v1/inbox?after=N`

```json
{"messages":[{"id":1,"say":"I checked it.","action":null,"params":{},"session_id":"..."}],"after":1,"has_more":false}
```

The top-level value is always an **object**, never a bare array or an `items`/`inbox` wrapper.
`messages` is always an array, `after` an integer and `has_more` a boolean. Every message has
integer `id`, string `say`, nullable string `action`, object `params` (never null), and nullable
string `session_id`. A broadcast reply can have `session_id:null`. Empty pages preserve the cursor:

```json
{"messages":[],"after":0,"has_more":false}
```

Speech is **not an audible backlog**. Summaries, setup/context notes and status-only sessions never produce spoken replies. Only the latest non-ended session for this device, active in the last 120 s, may receive non-empty `say`/an action. Historical/superseded/ended/broadcast items still advance their normal IDs/cursor but have `say:""`, `action:null`, and `params:{silent:true,suppressed:...,historical_say:...}` in addition to existing parameters. Cancelled turns are similarly silent. Do not read `historical_say` aloud. Floor-held new stack speech is not emitted until `quiet`. Clients must also filter to the currently human-started session and queue against actual local audio; server floor knowledge depends on these client events.

IDs monotonically increase across speech and actions. Start at 0; persist the returned cursor after
processing. Up to 100 messages/page; follow `has_more`. A message with `action:null` is speech only.
For non-null action, dispatch only a tool supported/declared by this client, validate params and permissions,
then report `tool_result` (failure as well as success). Unsupported/stale-session actions must not execute.
Use `say` as data for the existing Realtime model to interpret in its own words, not Android/browser TTS.
Append a data-only system item and request a tool-free response without overriding the session persona.
Optional `source` names the originating lookup/run/voice result and frames it as a continuation of requested work.
Lifecycle/empty-status rows retain IDs but are silent. Identical normalized partial findings deduplicate
across retries/restarts; a final answer replaces progress still held for the floor.
Do not speak over the user or an active Realtime response: queue until a safe turn boundary. Do not acknowledge
an action as complete until the actual device result is known. Deduplicate on message ID.

### `GET /v1/stream?after=N` (SSE)

Same messages as inbox; header auth required. Event `message`, SSE `id` equals message ID, `data` is
one message object. Accepts `Last-Event-ID` on reconnect if `after` is absent. Heartbeat comments every
15 seconds; reconnect with last processed ID. Native Android can use streaming HTTP; browser clients
must use `fetch` streaming (native `EventSource` cannot set Authorization). Max 16 streams. SSE and
inbox are read-only/replayable, not destructive queues.

### `GET /v1/sessions`

`{sessions:[{id,client,created,updated,ended}]}`: latest 100 sessions for this device, newest first.
Timestamps are Unix seconds, `ended` is 0/1. `GET /v1/context?session_id=...` also exposes latest context.

### `POST /v1/cancel`

`{"session_id":"...","turn_id":"optional exact turn"}`. Session ownership is checked before cancellation. Returns `{ok,status,turn_ids,say}`. `cancelling` means stops are requested; trace `cancel_confirmed` events name exact native run IDs and terminal statuses. No false terminal success when the native host is unavailable. `cancel_work` is the equivalent Realtime tool for all admitted work in the session.

### `GET /v1/trace?turn_id=...` (or `session_id=...`)

Returns `{events:[...]}` for the owning paired device only. Each observation has `turn_id,sid,layer,role,model,event,ts,ms,usage,cost_usd,details,cost_status`. Native lifecycle/runtime IDs and per-layer usage are included where supplied. Missing dollar billing is `cost_usd:null`, not zero. Local Hermes can use `z0live trace --turn ID` without another model call. JSONL snapshots are under `Z0LIVE_DATA/traces/` (0600); SQLite is canonical. This is observation/replay, not a second task ledger or proof of successful effects.

### `POST /v1/sessions/SESSION_ID/end`

Body `{}`. Flushes an outstanding compact summary as a **silent note to Hermes** and marks the session ended; safe to repeat.
Alternatively set `data.session_end:true` on the final event (e.g. `summary`). Always call at hangup.
If the phone disappears, the service independently flushes after 120 idle seconds, without deleting history.
Every 300 active-conversation seconds it also produces an extractive compact summary (last 16 salient
transcripts/results, counts, and all feedback since the prior summary); GPT-supplied summaries are preferred.
The service does not fabricate an LLM summary if none was sent. `Z0LIVE_SUMMARY_SECONDS` and
`Z0LIVE_IDLE_SECONDS` configure these intervals. Active time accumulates across transcript/tool events,
with individual gaps capped at 60 seconds; silence alone does not count as conversation.

## Errors/security

`400` invalid body/schema, `401` missing/revoked device token, `403` browser origin denied,
`404` unknown route or another device's session, `429` stream limit, `502` provider unavailable.
Health is liveness only, not a claim of upstream readiness. No query-token authentication. Loopback
listener only; Tailscale Serve is tailnet-only, not Funnel. Device tokens authorize owner-level delegation
through Hermes; pair only trusted devices. All paired devices belong to the same owner; direct messages
without session and seed replies broadcast intentionally. Browser cross-origin access is disabled unless
an exact origin is allowlisted with `Z0LIVE_CORS_ORIGINS` (comma-separated). No wildcard CORS.

## Garden Room migration (no Garden Room changes made)

A future Garden Room adapter can pair as `web`, mint via `/v1/realtime/session` declaring its existing
`get_app_state`, `app_action`, layouts and AR tools, and keep executing these locally in `voice.js`.
Use this broker's builtin tell_hermes/context/summary dispatch, normalized batched events and inbox/SSE;
retain Garden Room's mic/audio gesture and turn-continuation handling. Allowlist its exact HTTPS origin
in `data/z0live/service.env` as `Z0LIVE_CORS_ORIGINS=https://0.taila30dc.ts.net:8790` before testing.
Do not embed a permanent token in public JS; obtain it via pairing and keep it in an authenticated client
store. This is a separate adapter, not a change to the portable v0 WebSocket/VoicePlan protocol in PRs #6/#7.

# z0live desktop client integration guide (HTTP v1)

Part of [z0live issue #13](https://github.com/kvnloo/z0live/issues/13). The canonical source is [API.md](API.md). Examples under [examples/http-client](../examples/http-client/) are synthetic and **offline**. This guide is not a new server, endpoint or portable WebSocket implementation.

## Which transport?

| Surface | Contract | Ownership |
| --- | --- | --- |
| Desktop pairing / ephemeral mint / telemetry / inbox / Hermes delegation | Authenticated \`/v1\` HTTPS, no wildcard CORS | z0live desktop broker |
| Voice audio and Realtime tool messages | **Direct** client ↔ OpenAI WebRTC SDP/data channel | Native/web client with owner mic consent |
| Portable actor/VoicePlan protocol | Existing \`src/z0live\` WebSocket API | Separate from desktop v1; do not substitute shapes |
| Lawnchair | Native activity/service and local PhoneAction authorization | Hermes-owned [Lawnchair PR #14](https://github.com/kvnloo/lawnchair/pull/14) |
| Dash | Expo/React Native app currently uses its own Bun bridge and audio-chunk protocol | A z0live adapter requires a custom development client and separately qualified native WebRTC module |
| Garden Room | App's own local get_app_state/app_action/AR tools | Client-only, locally permission-checked execution; no broker power to operate AR/device UI |

See [DASH.md](DASH.md) and [TESTING.md](TESTING.md). The separate experimental [TypeScript client PR #24](https://github.com/kvnloo/z0live/pull/24) adds an injected HTTP implementation, but is **draft and NOT RUN**; until integrated, TypeScript calls below are **pseudocode**, not a shipped npm dependency.

## Pairing, secrets and lifecycle

1. Owner pairs a **trusted device** using desktop \`z0live pair DISPLAY_NAME\` or approved QR. Confirm the exact HTTPS broker origin before saving. Store the bearer only in the client's platform-protected store (Android Keystore / Expo SecureStore development build); browser applications must not embed a permanent bearer in public JS, query parameters or localStorage. Do not log pairing links: they are bearer credentials.
2. Supply \`Authorization: Bearer <injected-device-token>\` on **every** broker route, including \`/v1/health\`, \`/v1/inbox\` and \`/v1/stream\`. Use \`Content-Type: application/json\` for JSON POST bodies. Revoke the token on loss via desktop \`z0live revoke DEVICE_ID\`; revoked new requests return 401. Already-open SSE sessions recheck at heartbeat; do not promise immediate termination.
3. After explicit foreground microphone permission and visible owner Start, POST \`/v1/realtime/session\` with \`client: "lawnchair"|"assistant"|"dash"|"web"\` and only declared client-local \`tools\` in Realtime **flat** function format. The broker registers schemas; it does *not* execute app-local tools or grant any device permissions.
4. The response includes ephemeral \`value\`, Unix-seconds \`expires_at\`, \`model\`, \`voice\`, \`instructions_version\` and broker-generated \`session_id\`. The short expiry is a **connection mint deadline**, not the audio call lifetime. No host OAuth credential is returned to the device. Do **not** persist \`value\`.
5. Create native/browser \`RTCPeerConnection\`, attach actual mic input and output route, create \`oai-events\`, offer SDP, POST \`application/sdp\` **directly** to \`https://api.openai.com/v1/realtime/calls\` with the *ephemeral* bearer, set SDP answer. No broker voice proxy or supported consumer ChatGPT Voice-automation interface is assumed.
6. Final transcript notifications become broker events; flush an explicit summary if available and POST \`/v1/sessions/SESSION_ID/end\` with \`{}\` on End/hangup. Close media tracks/data channel/peer and foreground service. If connection fails, clean up before minting anew. Broker inactivity summary is a fallback, not permission for continuous capture.

## Tool routing and safe turns

- Realtime \`response.function_call_arguments.done\` may arrive **before** \`response.done\`. Parse its arguments against the declared schema. For builtins use **individual routes** \`POST /v1/tools/tell_hermes\`, \`/v1/tools/get_context\`, \`/v1/tools/summarize\`, each receiving \`{"session_id":"...","arguments":{...}}\`. There is **no** aggregate \`/v1/tools\` dispatch endpoint.
- For client-declared app tools, validate **locally** (target visibility, profile, permissions, user confirmation and fresh UI state), execute only supported actions, and send the actual \`conversation.item.create\` function-call output on the Realtime data channel. Logging \`tool_call\` to broker telemetry is NOT execution. Builtin \`tell_hermes\` returns \`status:"queued"\` only; it does not verify Hermes completion.
- Send a single \`response.create\` continuation **after** prior \`response.done\` and outstanding local tools finish. Prevent duplicate \`call_id\` completion from rearming a second response. If the user is speaking or Realtime is speaking, queue inbox speech until a safe boundary.
- Inbox \`say\` uses client phone TTS; temporarily suppress live mic and Realtime output during TTS, *without* clearing the user's mute preference. End/disconnect must dispose pending callbacks. Never claim physical echo-cancellation or barge-in success based on state tests.

## Event batches and replay

\`\`\`json
[
  {
    "session_id": "synthetic_session",
    "ts": 1790000000.25,
    "type": "user_transcript",
    "text": "synthetic text only",
    "data": { "event_id": "synthetic_unique_event_1" }
  }
]
\`\`\`

\`POST /v1/events\` receives a **bare array**, not \`{"events":[...]}\`; 1–100 records per batch, Unix seconds (ISO timestamp accepted), \`data\` object and client-stable \`event_id\` for SQLite dedupe. Server receipt time is separate. Store final transcripts, not raw mic audio/credentials or untrusted screenshots. Invalid batches fail atomically. If retrying telemetry after ambiguous delivery, reuse the **same** \`event_id\`; do not create a new ID.

\`GET /v1/inbox?after=N\` returns \`{messages:[{id,say,action,params,session_id}],after,has_more}\`. Null \`action\` is speech-only, \`params\` always an object, \`session_id\` nullable for broadcast. IDs monotonically increase; up to 100 per page, follow \`has_more\`. Only persist cursor **after** the item has been processed or deliberately rejected with a recorded outcome. A duplicate replay must NOT reexecute a destructive/local action. \`GET /v1/stream?after=N\` provides the same entries as authenticated SSE (\`event: message\`, \`id\`, JSON \`data\`). \`Last-Event-ID\` works when \`after\` is absent; explicit \`after\` takes precedence. An EventSource constructor cannot supply the required custom bearer; use authenticated fetch-streaming in browsers, appropriate native streaming on phone.

When a POST tool/mint/end times out, the operation may have been accepted before transport failed. Do not blindly retry execution or mint a duplicate session. Reconcile against existing session/inbox/job evidence; ask the owner before non-idempotent actions. Read-only GET/inbox/SSE reconnect may be retried with bounded backoff; beware duplicate delivery.

## Platform notes

**Browser:** exact \`Z0LIVE_CORS_ORIGINS=https://approved-origin.example.invalid\` needed for that HTTPS web origin; no wildcard allowlist or permanently distributed bearer. Do not use a public web bundle as a token vault.

**Dash / Expo:** [DASH.md](DASH.md) identifies current Dash Voice as chunked audio through its own Bun bridge, not native Realtime; stock Expo Go does not prove a native \`RTCPeerConnection\` integration. Confirm module, microphone lifecycle, secure storage and custom development build before a live test. Do not invent \`dash://voice\` production deep links.

**Garden Room:** client declares its own \`get_app_state\`, \`app_action\` and AR/tool schemas on mint, preserves local validation and action authority in \`voice.js\`, and uses broker builtin routes for delegation/context/summary. A local tool is never executed by posting \`/v1/events\`.

**Lawnchair:** [PR #14](https://github.com/kvnloo/lawnchair/pull/14) owns live native voice/foreground mic and local phone-tool implementation; this document does not authorize editing that branch. Distinguish Android system assistant selection from OpenAI Realtime.

## Deterministic fixture/validation gate (proposed, NOT RUN)

\`\`\`sh
python3 examples/http-client/validate_examples.py
python3 -m unittest discover -s examples/http-client -p 'test_*.py' -v
\`\`\`

The validator compares synthetic frames to the documented API's endpoint forms and rejects wrapped event arrays, bare inbox arrays, invalid cursors/queued status and forbidden query-token examples. \`example.py\` builds a request with an **injected** token and a fake transport; its default path does not send HTTP. No live network call, model, real account, app action, worker, phone, or provider has been executed.

**Optional future live smoke (all NOT RUN):** use an owner-approved test device, privately mint a disposable paired bearer, confirm \`GET /v1/health\`, then verify the concrete-session mint/event/inbox/end lifecycle with permission and cleanup. Keep private endpoint/token/logs out of Git; preserve source SHA, consent, failure receipts and rollback. A health 200 or queued job is *not* evidence of actual provider audio or phone execution.

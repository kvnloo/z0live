# Typed desktop HTTP client (experimental)

Part of [z0live #12](https://github.com/kvnloo/z0live/issues/12). This module wraps the desktop broker **HTTP `/v1` API**, not the separate portable `src/z0live` WebSocket/actor protocol. It has **no React dependency, Android executor, desktop-agent authority, provider credentials, token persistence, or microphone/WebRTC**.

## Isolated proposed checks

```sh
cd clients/typescript
npm install --ignore-scripts
npm run typecheck
npm test
```

**Tests NOT RUN — ChatGPT cannot execute code.** Hermes must execute against the exact SHA, log Node/TypeScript versions and separate typecheck vs tests. Do not publish this package; there are no root manifest or workflow changes. No native WebRTC or Expo Go path is assumed.

## Browser example (explicit HTTPS origin and in-memory bearer)

```ts
import { DesktopBrokerClient, memoryToken } from "./src/index.js";

const token = memoryToken("USER_ENTERED_DEVICE_TOKEN"); // synthetic placeholder
const client = new DesktopBrokerClient({
  origin: "https://YOUR_APPROVED_TAILNET_HOST:8791",
  token: token.provider,                  // never add bearer to a URL
  fetch: (url, init) => fetch(url, init), // injected; RN can provide its own adapter
  nowSeconds: () => Date.now() / 1000,
  // Optional injected cursorStore for explicit at-most-once client processing:
  cursorStore: { load: () => inMemoryAfter, save: after => { inMemoryAfter = after; } },
});
let inMemoryAfter = 0;
const inbox = await client.getInbox();
for (const message of inbox.messages) {
  // Authorize and perform any client-local action separately; failures are NOT success.
  // Confirm the result before marking this item processed.
  await client.acknowledge(message);
}
token.clear();
```

The SDK never calls a phone executor. `postEvents` logs observability events, not commands. Calling `tell_hermes` returns `queued`, not agent execution or phone verification.

## Methods and constraints

| Operation | Type / safety |
| --- | --- |
| `mint({client,context?,tools?})` | Runtime-validated ephemeral session secret, Unix-seconds expiry; **no auto retry** |
| `callBuiltin("tell_hermes"|"get_context"|"summarize",id,args)` | Runtime-validated typed queued result / context; **no auto retry** |
| `postEvents(events)` | Bare event array, bounded and type-checked; **no auto retry** |
| `getInbox(after?,{signal?,retries?})` | Validated cursor/message objects; optional **max two** retries for network/5xx GET only |
| `listSessions/getContext` | Typed parse and client-supplied metadata; no phone-screen access |
| `endSession(id)` | Explicit hangup signal; **no auto retry** because delivery may be uncertain |
| `stream(after,signal?)` | Authenticated fetch-stream SSE; TextDecoder preserves split UTF-8; IDs validated; caller reconnects with last **processed** cursor |
| `acknowledge(message)` | Explicit injected cursor save **after** caller processing; no default localStorage |
| `memoryToken(...)` | Mutable per-process token provider with `clear()`; callers own pairing and disposal |

AbortSignal supported for each request; a bounded timeout and redirect refusal are enforced. Only an exact `https://` origin is accepted (no credentials, paths, queries or fragments), no cross-origin follow or default cookies. Native clients must inject compliant `fetch` and optional storage; browser CORS may require approved origin. A native WebRTC transport is intentionally **not implemented**.

## Error semantics

`BrokerError.kind`: `configuration`, `protocol`, `auth` (401), `http` (other non-2xx/redirect), `network`, `aborted`, `timeout`. HTTP status is retained but bearer, headers, and server body are not reflected in messages. App-local permissions, unsupported tools, stale sessions and `queued` are distinct from verified completion. A timed-out mutation is uncertain: obtain an independent receipt or user approval before replay; never silently remint/reexecute.

Known bounds: server replay is at-least-once and device-scoped; this SDK cannot atomically commit a local phone action and cursor. In-memory cursor loss after process death will replay messages. Provider audio/device/worker qualification: **NOT RUN**. See [desktop API](../../docs/API.md).

# Inbox pagination and SSE replay conformance

Part of [z0live #11](https://github.com/kvnloo/z0live/issues/11). Read the [desktop v1 API](../API.md) for the bearer-authenticated consumer contract. **Tests: NOT RUN — ChatGPT cannot execute code.** Hermes runs against the exact SHA:

```sh
python3 -m unittest discover -s desktop/tests -v
```

The test creates a temporary real `Store` and loopback HTTP `Server`/`Handler`; upstream mint is patched. No Hermes worker, provider, phone, live audio or external host is involved. All HTTP connections use a five-second socket timeout; SSE reads stop at a complete first message or after 16 lines. Every test closes the server and signals its request threads to stop.

## Expected protocol observations

| Input | Expected output |
| --- | --- |
| `GET /v1/inbox?after=0` on empty store | Object `{messages:[],after:0,has_more:false}` |
| 103 published messages | First 100 ordered by increasing ID, `has_more:true`; next 3 with `has_more:false` |
| Exhausted cursor | Empty `messages`, unchanged numeric `after` |
| Broadcast and owner-scoped messages | Broadcast visible to both paired devices; owner message only to originating device |
| Speech-only inbox entry | `action:null`, `params:{}`, nullable `session_id` |
| Inbox entry with action/params | Declared metadata returned; **no local execution** by broker |
| `GET /v1/stream?after=0` | `event:message`, SSE `id` and JSON `data` match inbox item |
| Reconnect using `Last-Event-ID` | First event strictly later than acknowledged ID |
| Both explicit `after=0` and `Last-Event-ID` | Explicit query cursor takes precedence |
| Reconnect from previous `after=0` | Earlier message repeats: client must dedupe by numeric ID |
| Absent/revoked bearer | New HTTP inbox and SSE requests return 401 |
| Bearer only in query string | Request returns 401; URL token is **not** authentication |
| Unapproved `Origin` header | 403 for inbox and SSE |

### Boundaries not established by this PR

- SSE is a **replayable transport**, not a destructive queue or an action result. Message arrival, server HTTP 200 and duplicate replay are not permission, local tool execution, acknowledgement or device verification. A consumer must persist the cursor **after processing** and independently enforce action eligibility.
- Existing SSE code rechecks device revocation at a heartbeat (nominally 15 seconds). No injectable clock exists in `Handler.stream`, so this suite checks new connections post-revocation only; immediate termination of already-open streams and heartbeat timing are **NOT RUN**. Do not add flaky sleeps.
- 16 concurrent stream limits, slow-reader backpressure, mobile network disconnect, app lifecycle and live long-duration streaming require separate qualification. No native EventSource-with-custom-Authorization-header assumption: use authenticated fetch streaming in a web client or suitable native HTTP transport.
- Tests use synthetic text/action names only; no real phone, installed-app data, credentials, audio or owner transcripts.

Hermes captures command, Python version, actual test results and exact head SHA in the issue body before integration.

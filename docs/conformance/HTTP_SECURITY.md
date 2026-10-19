# Desktop HTTP authentication and request-boundary conformance

Part of [z0live #19](https://github.com/kvnloo/z0live/issues/19). Source observed: \`desktop/z0live_desktop/server.py#Handler.handle_api/reply/do_OPTIONS/stream\` and \`core.Store.authenticate/new_session/add_events/tool\`. The broker binds loopback, with Tailscale Serve as a separate private routing layer. Neither loopback nor a matching HTTP Origin grants auth. No new production permissions are proposed.

## Proposed offline gate

\`\`\`sh
python3 -m unittest discover -s desktop/tests -v
\`\`\`

**Tests NOT RUN — ChatGPT cannot execute code.** The new suite uses a real \`ThreadingHTTPServer\` on \`127.0.0.1:0\`, temporary \`Store\`, two synthetically paired device bearers and a patched provider mint. Each response/body read has a five-second HTTP timeout; the test signals stop, closes server and joins its serving thread. No hosted token, Tailscale endpoint, provider, Android phone or Hermes worker is invoked.

| Threat / input | Contract expectation | Source |
| --- | --- | --- |
| Missing/wrong/revoked bearer, even \`/v1/health\` | HTTP 401 with sanitized static text; zero session/event/job side effect | \`handle_api\`, \`authenticate\` |
| Bare token in URL/query, without auth header | HTTP 401, no token echo in response | \`handle_api\` |
| Browser \`Origin\` differs from exact allowlist | HTTP 403 independently of bearer; no wildcard CORS headers | \`handle_api\`, \`reply\` |
| Explicit allowed origin | Normal bearer validation still required; exact \`Access-Control-Allow-Origin\` and \`Vary\` on responses | \`reply\` |
| OPTIONS preflight allowed/disallowed | 204 for allowlisted origin, 403 otherwise; only declared \`Authorization\`, \`Content-Type\`, \`Last-Event-ID\` allowed | \`do_OPTIONS\` |
| Separate paired device accesses first device's session | 404 / no job, even with valid own bearer | \`Store.context\`, \`Store.tool\` |
| Non-JSON, invalid content type, empty/oversize POST body, invalid envelope | HTTP 400, no mutated SQLite state | \`handle_api\` |
| Oversize text, 33 tools, 101 event records | HTTP 400, no session/event/job side effect | \`Store.new_session/add_events/tool\` |
| More than 16 SSE streams | HTTP 429, no persistent job/event side effect | \`stream\` |
| Query secret in HTTP log | Only path is logged, not query/header/bearer; tests inspect logger call arguments | \`log_message\` |

**Known boundary:** \`Handler.reply\` currently includes a truncated \`str(exc)\` (max 200 chars) on client 400 errors and RuntimeError 502 errors. This is an existing source pattern, not a proof every future exception message is non-sensitive. Tests ensure synthetic bearer text does not appear in exercised responses. If a future error includes secrets, Hermes owns redaction at source; do not weaken authentication or hide failures.

- Repeated authentication checks on established SSE take place at heartbeat / delivery, nominally 15 seconds; this test does not claim immediate revocation of an active stream.
- These are **local loopback** controls. Public reachability, Tailscale Serve vs Funnel, firewall, certificate chain, real browser CORS enforcement and resource exhaustion beyond bounded fixtures remain **NOT RUN**.
- Invalid JSON/event bodies, alone, are not exploit demonstrations. Logs and private device files remain local; do not put authorization, trace/body/query strings in Git.
- Do not confuse HTTP accept, \`status: queued\` or bearer rejection with agent action completion or confirmed phone permission.

Hermes should record full head SHA, Python version, proposed command result, any failure/rework and release owner go/no-go before integrating.

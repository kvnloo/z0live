# Desktop broker builtin / client-tool conformance

Part of [z0live #9](https://github.com/kvnloo/z0live/issues/9).
This is a **contract test plan**, not proof of executed commands, hardware behavior or agent completion.

## Proposed Hermes gate

```sh
python3 -m unittest discover -s desktop/tests -v
```

**Tests NOT RUN — ChatGPT cannot execute code in this lane.** Hermes should record exact commit SHA, Python version, isolated test outcome and regression/fix receipts in issue #9. No account token, provider, remote Hermes process, device or audio is needed.

Test isolation: `tempfile.TemporaryDirectory` Store; two synthetic paired device tokens; patched `core.mint` for all session creation; loopback `Server`/`Handler` with ephemeral TCP port and explicit shutdown. No worker process is started. Tokens/identities are generated solely in the temporary fixture and never committed.

## Contract matrix

| Boundary | Required result / negative control |
| --- | --- |
| Live builtin catalog vs actual `Store.tool` branches | Each declared builtin has a corresponding dispatch route; names are read dynamically from `core.TOOLS` and parsed from the actual dispatcher, not copied into a separate closed name list |
| Registered app tool vs builtin | App tool is session schema only; direct broker dispatch is `LookupError` / HTTP 404; `tool_call` and `tool_result` events are inert audit records |
| `tell_hermes` valid kind/text | New `direct` job is `queued`, with originating session and no run ID; never device-verified or completed |
| Invalid kind/text/body/arguments | `ValueError` / HTTP 400; no job |
| Foreign session or device bearer | `LookupError` / HTTP 404; no job |
| Missing bearer | HTTP 401, including `/v1/tools/*` |
| `get_context` | Only submitted session metadata; latest `app_state` replaces stale initial context |
| `summarize` | Durable summary event and `summary` job with status `queued`, not completion |
| Unknown builtin and local-only names | `LookupError` / HTTP 404 |
| App tool limits | 32 allowed; 33 rejected before mint; max serialized tool catalog 48000 chars |
| Reserved names, duplicates and malformed schemas | Rejected before mint; mandatory builtin names cannot be overridden |
| Invalid client or oversized/incorrect context | Rejected before mint |

### Known validation scope

- The broker validates the app-tool registration envelope (`type=function`, name regex, object `parameters`, uniqueness and caps). It does **not** implement a general JSON Schema validator for builtin call arguments; `tell_hermes` specifically checks kind/text, `summarize` checks text, and `get_context` returns metadata. Do not present registration as phone action authorization.
- Static route comparison examines direct `if name == "..." ` branches in the current `Store.tool`. If Hermes changes dispatch architecture, the assertion should be adapted to the real route registry; it must not be weakened by hardcoding only old builtin names.
- The HTTP test asserts current error mapping: malformed input 400, unpaired 401, foreign/unknown 404, queued 200. A response of `queued` proves only accepted persistent work, not actual agent execution.
- This suite does not execute real Hermes jobs, OAuth mint, OpenAI Realtime, local Android tools, Dash, Garden Room, or physical audio. Provider/runtime/device qualification remains **NOT RUN**.

Source: [API contract](https://github.com/kvnloo/z0live/blob/main/docs/API.md); tests mirror existing `desktop/tests/test_broker.py` server/Store patterns. New voice-tool implementations on `main` remain Hermes-owned.

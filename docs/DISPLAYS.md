# Displays: the globe a call can drive

A display is a browser tab the broker can drive: today the God's Eye View (GEV)
globe ([kvnloo/gods-eye-view](https://github.com/kvnloo/gods-eye-view), branch
`feat/z0live-display`, `docs/Z0LIVE-DISPLAY.md`). When the owner says "take me
to Tokyo" or "show flights over Texas" in a call, the live model calls the
`globe` builtin. The broker hands the command to the tab and returns the tab's
own result. Hermes agents reach the same display through `bin/z0live globe` or
the plugin's `z0live_globe` tool.

Code: `desktop/z0live_desktop/displays.py`. The registry lives in memory: a
broker restart forgets displays until their next heartbeat (≤5 s) registers
them again.

## Display side

Every route needs a paired-device bearer token (voice-scope tokens are refused).

| Route | Body | Result |
| --- | --- | --- |
| `POST /v1/displays/register` | `{display, tools:[{name, description}], context}` | `{ok, display, stale_after:15, result_timeout:12, generation}` |
| `POST /v1/displays/<name>/heartbeat` | `{context?}` | `{ok, pending}`; send every ~5 s |
| `POST /v1/displays/<name>/context` | `{context}` | `{ok, pending}`; at most 1/s |
| `GET /v1/displays/<name>/inbox` | – | SSE: `id: <cmd>`, `event: command`, `data: {id, action, args}`; `: heartbeat` comments every 10 s |
| `POST /v1/displays/<name>/result` | `{id, ok, output}` | `{ok, accepted}`; `accepted:false` for unknown or expired ids |

`display` matches `[a-z][a-z0-9_-]{0,31}`. A display accepts 1–64 tools with
unique names. Context is an object of at most 16000 characters. Only the
device that registered a display may heartbeat it, read its inbox or answer for
it (404 for any other device). Another paired device can take the name over
only after it goes stale (15 s without a heartbeat). Re-registering from the same device
(a page reload) keeps queued commands.

## Caller side

| Route | Who | Body |
| --- | --- | --- |
| `POST /v1/tools/globe` | the phone, like every builtin | `{session_id, call_id?, arguments:{action, args?}}` |
| `POST /v1/tools/globe_context` | the phone | `{session_id, arguments:{}}` |
| `POST /v1/displays/<name>/command` | owner agents (`bin/z0live globe`, plugin) | `{action, args?}` |
| `GET /v1/displays/<name>` | owner agents | – |

Phone routes check session ownership as the other builtins do; voice-scope
tokens may call `globe` and `globe_context`. A command waits for the display's
result for up to 12 s (`Z0LIVE_DISPLAY_RESULT_SECONDS`) and returns:

- `{ok:true, status:"done", id, output}`: the display reports success;
- `{ok:false, status:"failed", id, output}`: the display ran it and it failed or was refused;
- `{ok:false, status:"timeout", id, output}`: no answer in time. The output says the action may or may not have happened;
- `{ok:false, status:"no_display"}`, `"unknown_action"` (with `actions`) or `"busy"` (16 queued).

A command that expires before the display reads it is dropped, never run
late. A result that arrives after the timeout is not accepted.

## Mint

While a `gev` display is alive at mint (Realtime transport), `builtin_tools`
gains two names and the session gets their schemas:

- `globe`: `{action: enum of the registered action names, args: object}`, its
  description lists each action's one-liner;
- `globe_context`: no arguments, returns the current view.

The persona then adds a short paragraph: use `globe` for "show me / take me
to" requests, and claim a change only on an `ok` result. With no live display,
neither tool nor the paragraph appears, so the tool budget is unchanged.
`globe` and `globe_context` are always reserved names: an app tool cannot claim them.
Frameless Bidi sessions have no function tools and do not get them. Phones
need no change: they already send every returned builtin to `POST /v1/tools/<name>`.

## Agents

```sh
bin/z0live globe                                   # connected? actions + current view
bin/z0live globe fly_to_location '{"query":"Tokyo"}'
bin/z0live globe set_layer_visibility '{"layerId":"flights","enabled":true}'
```

The CLI speaks HTTP to the running broker (`Z0LIVE_BROKER_URL`, default
`http://127.0.0.1:$Z0LIVE_PORT`, port 8791). It authenticates with
`Z0LIVE_TOKEN` or the private token file `Z0LIVE_TOKEN_FILE` (default
`~/.config/z0live/hermes-mobile-relay.token`, the Hermes relay device the macOS
installer pairs). The token is never printed. It exits 1 unless the result is ok.

The Hermes plugin tool `z0live_globe` (`{action?, args?}`) does the same. It
reads the `Z0LIVE_TOKEN` profile secret, else the `device_token_file` config
key; `broker_url` sets the broker. The tool is hidden when no token is available.

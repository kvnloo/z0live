# Desktop broker verification receipts

Verified on groot through **https://0.taila30dc.ts.net:8791**, with real host ChatGPT OAuth,
OpenAI WebRTC and the native Hermes API adapter. No provider credentials are printed or committed.
This deployment adapter leaves the portable z0live v0 runtime/fixtures and z0evals certification work intact.

## Commands and results

```bash
ruff check desktop bin/z0live
ruff format --check desktop bin/z0live
python3 -m unittest discover -s desktop/tests -v
python3 -m compileall -q desktop
node --check desktop/tests/live_webrtc.mjs
node desktop/tests/live_webrtc.mjs
python3 desktop/tests/live_recovery.py
python3 desktop/tests/live_inbox.py
```

- Ruff passed; nine deterministic tests passed. These tests explicitly mock minting and do not prove live provider access.
- Real authenticated `/v1/health`: `{"ok":true,"version":"1.0.0"}`; no bearer: HTTP 401.
- `/v1/realtime/session`: real client secret length **35**, model `gpt-realtime`, voice `marin`,
  instructions version `z0live.persona.v1`. Secret value never printed.
- Real Chromium/OpenAI `/v1/realtime/calls`: **SDP 201**, open WebRTC data channel,
  registered tools `tell_hermes`, `get_context`, `summarize`, `local_test`; actual GPT `tell_hermes`
  function call; assistant continuation 136 characters; zero Realtime errors.
- Broker session `d32d882d4c4f486fa21a935a858d095c`: actual transcripts/tool telemetry accepted;
  session end queued service-generated extractive summary job **8**.
- Actual Hermes reply at inbox ID **8**: `Z0LIVE_E2E_1792393870199`.
- Summary job **8** completed in native session `z0live`, run `run_62bf213452d0479c83dd959a918c018f`.
  Native journal confirms `anthropic/claude-opus-5-5`, not a mocked agent response.
- `/v1/sessions` returned this device's ended session; authenticated `/v1/stream` replayed its real reply.
- Recovery session `28476979879b4725895c4a1d8d747bd4`: `/v1/tools/get_context` returned
  `{"app":"headless-recovery"}`. CLI queued `local_echo` inbox ID **10**; a real harmless headless-local
  handler produced `{"echo":"headless-local-receipt"}` and posted its tool-result receipt. Not a phone action.
- `/v1/hermes/send` accepted job **9**; restarted broker while native run
  `run_ab35f35882bc4da0aac97786055a541b` was admitted. The same run completed and reply ID **11** said
  `DIRECT_RECOVERY_1792393924390846012`; no duplicate run or lost reply.
- Feedback job **10** and supplied-summary job **11** completed, respectively native runs
  `run_376dc262b5844adcb6e62d60b6dafbef` and `run_4be1b05b99be4d3383a39e205980090d`.
- Both services restarted; health/auth/history persisted; seed count stayed **1**. Both remain enabled.
- Data directory mode **0700**; device verifiers, native API key and broker database mode **0600**.

## Exact live inbox framing

The dedicated real HTTPS transport test published only to its temporary device; it did not pretend this
message was a Hermes response. The complete endpoint JSON was:

```json
{"messages":[{"id":14,"say":"Z0LIVE_INBOX_CONTRACT","action":null,"params":{},"session_id":null}],"after":14,"has_more":false}
```

The subsequent empty page was:

```json
{"messages":[],"after":14,"has_more":false}
```

After CLI revocation the same token returned **401**, and its stored verifier read back as revoked.
The top-level object, integer cursor, boolean pagination flag, nullable action/session ID and object params
match [API.md](API.md). Regression tests cover empty/populated HTTP pages and 101-message pagination.
The current sibling Lawnchair `Z0LiveProtocol.inboxPage` reads this same `messages`/`after`/`has_more` contract.
No backend contract rename was made to accommodate an older parser.

## Limits

Headless text-mode WebRTC proves real transport, GPT tool dispatch and Hermes delivery, not physical
Android microphone capture, speaker routing, background capture policy or permission-gated Android actions.
The live WebRTC/recovery tests leave private history and test-device verifiers as receipts; revoke unused
test devices with `z0live devices` / `z0live revoke DEVICE_ID`. `live_inbox.py` revokes its own device.
For reproduction, use the current tool output rather than assuming the IDs above repeat.

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

## Persona v2 / backend requalification (2026-10-19)

- Commit `5a95e5f` introduced `z0live.persona.v2`: accurate Realtime/Hermes architecture,
  local-tool-first routing, truthful result/permission language, composer-only call/SMS handling,
  explicit accessibility limits and no screen-reading claims. Mint instructions include the exact
  current builtin + app tool schemas, names and descriptions; no hardcoded app-tool inventory.
- Nineteen deterministic desktop tests passed, including three persona/mint tests. Synthetic OAuth
  and mocked upstream are explicitly confined to unit tests, not used as live evidence.
- Restarted only the exact `z0live.service` checkout after checking zero unended sessions and zero
  queued/running jobs and coordinating with the parent. Native Hermes host was not restarted.
- Ran the existing real `desktop/tests/live_webrtc.mjs` against HTTPS: real v2 mint, model
  `gpt-realtime`, voice `marin`, secret length 35 (value never printed), direct OpenAI WebRTC SDP
  201, actual GPT `tell_hermes`, zero Realtime errors, authenticated inbox and SSE replay.
- New isolated session `9d893f3b8a334006913bfbeb6f2b5813`: direct job 13 completed in
  `run_aed10417cd7f4b49a5a74878d402e31d`, inbox 16 contained the requested test marker;
  transcript-derived end-summary job 14 completed in `run_84153f8aa58f478dbfe4bedd8d517a78`,
  with a separately verified session/device-scoped inbox reply 17. No synthetic live replies.
- Ended the test session, revoked only its new `headless-live-*` device and read back revocation.
  All preexisting device records remained exactly equivalent after JSON decoding, including
  the owner's pairing. No phone driving, Android changes, owner messages or Telegram delivery.
- Sanitized logs and cleanup/job/inbox receipts:
  `/mnt/zer0models/project-artifacts/z0live/2026-10-19/backend/`.
- This run registered the three then-live builtins plus `local_test`; concurrent new backend tools
  and Android tools were not yet deployed and are not claimed as live-verified here. Dynamic unit
  assertions compare against `TOOLS` so later builtin additions are included automatically.

## Limits

Headless text-mode WebRTC proves real transport, GPT tool dispatch and Hermes delivery, not physical
Android microphone capture, speaker routing, background capture policy or permission-gated Android actions.
The live WebRTC/recovery tests leave private history and test-device verifiers as receipts; revoke unused
test devices with `z0live devices` / `z0live revoke DEVICE_ID`. `live_inbox.py` revokes its own device.
For reproduction, use the current tool output rather than assuming the IDs above repeat.

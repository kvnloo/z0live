# Multi-agent stack verification

## Scope and provenance

Qualified 2026-10-19 on the desktop with real headless Chromium → OpenAI WebRTC text turns, tools-free Haiku inference, the existing persistent native Opus session, and native Codex Sol child runs. Runtime checkpoint: `c08550b14429579c9645407d2ca242bab123d800`. The subsequent note-silencing change is additionally unit/legacy-transport tested; it does not change `ask_z0`/fanout. Canonical plan export: z0intelligence `d60f79ceded28cec813ae28ed1c0b4e08fb7ba98`, [PR #174](https://github.com/kvnloo/z0intelligence/pull/174).

Real tests used a separate loopback broker on port 18791 and a private scratch data directory. Its L2 commands explicitly selected that data directory. No test progress was put into the phone's inbox. Test bearer credentials are revoked by the stack harness. Private full output is retained locally at `cache/scratch/z0live-stack-qualification/live-final.json` under the active clean profile, with a reduced `metrics.json`; neither contains a committed provider credential and neither private transcript artifact is published here.

```sh
# Offline, fakes per layer; no provider/model credentials required:
Z0LIVE_VOICE_PLAN=/nonexistent nice -n 10 python3 -m unittest discover -s desktop/tests -v
nice -n 10 ruff check desktop bin/z0live

# Opt-in: point these at an already prepared, isolated broker/data directory:
Z0LIVE_DATA=/private/qualification/broker \
Z0LIVE_TEST_BASE=http://127.0.0.1:18791 \
Z0LIVE_TEST_OUTPUT=/private/qualification/live.json \
  nice -n 10 node desktop/tests/live_voice_stack.mjs
```

The deterministic suite passes **54 tests**, including baseline broker/pairing/persona/work-tool coverage and stack cache/idempotency/ownership, immutable roles, native model lock policy, two-worker execution, worker timeout, lost-acceptance uncertainty, cancellation, floor queuing, atomic batch rejection, saved-result restart recovery, usage scope, summary/context/feedback-note silence, status-only session suppression, and ended/superseded-session backlog suppression. Hosted Desktop broker CI also runs on Python 3.11 and 3.14. z0intelligence's 2 profile tests, profile export and hosted checks passed.

## Real scenarios and latency (one final run, not percentiles)

| Scenario | L0 first text / completed response | Broker work HTTP | L1 inference | L2 native elapsed | L3 native elapsed | Result |
|---|---:|---:|---:|---:|---:|---|
| Model identity | 1,030 / 1,440 ms | 70 ms | **24.7 ms cache**, no inference | none | none | Answer came from the pinned plan |
| Memory question | 865 / 1,015 ms | 71 ms | 2,017 ms brief; 1,581 ms condensation | 11,394 ms | none needed in final run | Actual history/evidence answer; gaps/approval uncertainty retained |
| All open PRs, Lawnchair + z0live | 1,220 / 1,385 ms | 76 ms | 1,448 ms brief; 1,765 ms condensation | 62,512 ms, including fan-out | **35,552 and 45,052 ms**, overlapping | Two distinct native Sol runs, current read-only PR context and decision summary |
| Interrupt mid-fan-out | 1,416 / 1,576 ms initial response | 68 ms | 1,370 ms brief | cancelled | **both native child runs cancelled** | All three exact native terminal states read back; test observed cancellation in **964 ms** after barge |

Initial z0intelligence evidence lookup took 60 ms for memory and 50 ms for PR/cancellation turns. Waiting for the asynchronous memory answer after the initial voice acknowledgement took 15,190 ms; the PR answer took 65,951 ms. The L2 times include native tool/model work; child times overlap and must not be added as serial latency.

**Budgets:** cached answer/admission is fast and all four final-run first-text measurements were below 1.5 s. Full Haiku calls **miss the 800 ms target**. Earlier repeated runs varied, including a first-text acknowledgement above 1.5 s and a cold memory investigation with additional workers taking over two minutes. No SLA/p50/p95 claim is made. Text-channel first text is **not first audible speech**; microphone capture, physical Android speaker/TTS, echo, acoustic barge-in and phone background policy are **not qualified** by this test.

## Supplied token usage and dollar-cost limitation

These are sums of actual provider/native usage observations per test turn, not guessed tokenization. Input counts include repeatedly presented cached system/history context. Cache reads are shown separately; they are already included in input, not additional tokens.

| Turn | L0 input/output | L1 input/output | L2 input/output (cache read) | L3 aggregate input/output (cache read) | Dollar cost |
|---|---:|---:|---:|---:|---|
| Identity | 4,763 / 78 | no inference | none | none | unknown |
| Memory | 5,202 / 49 | 1,251 / 161 | 345,581 / 486 (336,978) | none | unknown |
| PR fan-out | 5,663 / 91 | 1,551 / 205 | 366,722 / 1,357 (354,296) | 363,206 / 2,956 (316,416) | unknown |
| Interrupted work | 6,208 / 105 | 900 / 98 | **not supplied on interruption** | **not supplied on interruption** | unknown/incomplete |

The ChatGPT/Codex/Anthropic paths return usage but not the owner's billed per-turn dollars or authoritative subscription prices. Thus traces contain `cost_usd:null` and an explicit unknown status. Null is **not zero**. The cancellation utterance's separate L0 response is retained in the full test session artifact but has no new work turn ID. Aborted provider calls may have consumed tokens despite missing native accounting. Worker system/tool context is currently large; reducing it is an open performance/cost optimization, not claimed as done.

## Actual trace examples

```sh
Z0LIVE_DATA=/private/qualification/broker z0live trace --turn turn-268c658775364fc9808eea2c
Z0LIVE_DATA=/private/qualification/broker z0live trace --turn turn-3f4b023c80bf4c25afd42330
Z0LIVE_DATA=/private/qualification/broker z0live trace --turn turn-8bf9647b240e438d853c7805
```

Selected actual fields (not invented provider responses):

```json
{"turn_id":"turn-268c658775364fc9808eea2c","layer":"L1","role":"smol","model":"claude-haiku-4-5","event":"cache_hit","ms":24.7,"details":{"cache":"pinned_voice_plan","inference_calls":0}}
{"turn_id":"turn-3f4b023c80bf4c25afd42330","layer":"L3","role":"task","model":"gpt-6.1-sol","event":"completed","ms":35552,"run_id":"run_24053fcb1e7c4b44ae4d25a8539667dd"}
{"turn_id":"turn-8bf9647b240e438d853c7805","layer":"L3","event":"cancel_confirmed","run_id":"run_a984ed5168484fb296d69b2641c01fda","native_status":"cancelled"}
{"turn_id":"turn-8bf9647b240e438d853c7805","layer":"L3","event":"cancel_confirmed","run_id":"run_86e9628e8bc0474eb4336713e01e5148","native_status":"cancelled"}
```

The snippets flatten selected nested `details` fields for readability; the actual CLI/HTTP schema is documented in API.md. Native served provider/model matched all pinned roles. Jev was not used. These receipts establish model execution, not independent verification of every semantic statement in an agent's PR recommendation.

## Phone integration and remaining limits

- No new pairing, provider credential, microphone permission or audio proxy. Mint/tool/inbox wrappers remain compatible. Dispatch the returned `builtin_tools` list; static lists need `ask_z0` and `cancel_work`.
- For full floor/barge semantics, the phone must forward `speech_started/stopped`, `response.created/done`, explicit cancellation/topic changes, and an active-session heartbeat when listening quietly. Keep its local TTS quiet boundary and clear interrupted-turn speech. Server-side silence is not a substitute for local audio state.
- Existing clients that do not forward barge state can still cancel via a final “never mind”/`cancel_work`, but provider audio interruption alone cannot cancel desktop workers.
- Historical/broadcast/ended-session inbox items are now silent while preserving IDs/cursors and historical parameters. Summaries and notes-to-Hermes are not spoken. Native task/approval authority remains Hermes-owned; read-only child prompts are not a terminal sandbox.
- The canonical plan was installed privately at `~/.z0int/config/z0live-voice-plan.json`; the desktop services were restarted only after deterministic tests passed. An authenticated production mint/cache probe read back the exact digest and all ten builtin names. A real HTTPS inbox/revocation check confirmed silent broadcast framing. No test progress went to the owner's phone.
- No default-branch or portable PR #6/#7 merge, phone APK install, real microphone test, hidden model fallback, GPU promotion, Telegram test message, or other-profile modification was performed.

## Issue #44: read-only lag investigation and routing correction

The inspection window was 2026-10-22 04:09:10.975 UTC to 2026-10-23 04:09:10.975 UTC.
The deployed source was `e29d564824f3d271331a8186fbf576c06ee6da49`.
SQLite contained three ended Lawnchair/Bidi sessions, 22 events, two user transcripts,
one phone delegation and no new `voice_trace` entries. This was not the legacy Haiku/Opus/Sol
trace path. The single delegation spent 3,009.7 ms selecting its route and 2,151.3 ms on
a phone step. Its `phone_ui_tree` receipt reported `blocked_app`, not a successful observation.
Total delegation time was 5,182.8 ms. These are one observed turn's stage times, not acoustic
latency or a distribution.

The router admitted a screen read from an app-related information question without an explicit
screen request. It now removes `phone_ui_tree` from liaison candidates unless the request asks
for screen observation or a phone operation. Bidi persona v4 and native requests receive the
originating session's declared client and transport. These connection facts do not describe the
phone's foreground app. No identity-specific reply cache or provider change was added.

No held speech rows existed. The one measurable heartbeat interval was 22.409 seconds.
The one phone inbox action received its result after 2,119.8 ms. No speech-start or speech-stop
edges were recorded, even in the session with two transcripts. Thus the logs cannot establish
a missed-utterance rate, microphone capture, end-of-speech latency or actual playback.
The short silent sessions do not prove that a spoken utterance was lost.

The discovered groot Dash bridge journal reported voice unavailable because its STT model was
absent. It contained no `voice.heard` or `voice.failed` rows. Read-only MBP inspection found no
Dash user unit, running Bun bridge, port 4747 listener or matching journal rows in this window.
The CPU Parakeet queue hypothesis in `Z0L1-LAG` remains unmeasured, not this incident's root cause.
Raw owner transcripts and session identifiers remain private.

Added regression cases cover UI-tree scope, unchanged tool catalogs, connection metadata,
exact-session context and existing native/realtime request framing. Tests, builds, lint,
provider calls and acoustic checks were **not run** for this correction. No service or phone
changed. The correction addresses the evidenced misroute only. General lag and missing speech
remain unqualified until a client supplies correlated onset, end, dispatch and playback edges.

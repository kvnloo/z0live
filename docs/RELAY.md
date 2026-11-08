# Relay to Hermes

"Pass it along", "tell Hermes", "send it to Hermes" and "let Hermes know" mean one thing: put
a message into Kevin's Hermes session. It is never a phone or screen action.

## Path

```text
voice (bidi delegation, or ask_z0 on gpt-realtime)
  → z0live-voice Hermes run (input = request + the last 16 spoken turns as data)
  → bin/z0live relay --session BROKER_SESSION "MESSAGE" [--to "title words"]
  → desktop/z0live_desktop/relay.py   resolve target (read-only SessionDB)
  → desktop/hermes_relay.py           Hermes runtime, gateway client protocol
       session.resume → running: session.steer (lands in the turn's next tool result)
                        idle or steer refused: prompt.submit (durable FIFO, next turn)
  → final reply = the command's "say": Sent to <title>: "<message>"  (voice reads it verbatim)
```

- **Target.** Default: Kevin's main session, the most recently active `cli`/`tui`-created
  session with at least 20 messages (probes, subagents, cron, one-shots and the voice's own API
  sessions are excluded). `--to` takes a session id or prefix, or title words.
  `Z0LIVE_RELAY_DEFAULT=<session id>` pins the default.
- **Message.** Stored as `[Relayed from Kevin's z0live voice call, HH:MM] MESSAGE`.
- **Idempotency.** `submission_id` is derived from target, text and minute (or `--key`), so a
  retried command does not queue a second turn.
- **Profile.** `Z0LIVE_RELAY_HOME` (default `~/.hermes/profiles/clean`). `HERMES_HOME` is set on
  the helper child process only.

## Why not `/v1/runs`

Same finding as Dash's continuation seam (`bridge/src/hermes-continue.ts`): the session that
owns a CLI/TUI chat is the gateway, and other clients submit into the gateway FIFO.

- The API server refuses `/v1/runs` for sessions that another surface owns (HTTP 409
  `permission_denied`, per the Hermes API docs).
- On the multiplexed listener (`127.0.0.1:8642`), `/p/clean/...` needs the clean profile's own
  `API_SERVER_KEY`, which it does not have (HTTP 401).
- The unprefixed `/v1/runs` with a clean session id was accepted and ran in the **default**
  profile under the same id (measured 2026-10-21). That id collision then wedged the real clean
  session: `session.resume` returned `admission_conflict`, and its TUI turn hung. Never probe
  this route with a real session id.

## Read-only session summaries

`bin/z0live sessions --limit 8` lists recent interactive sessions, each with title, source, age,
Kevin's last own message, the current activity and the number of active helper agents. The voice
backend uses it for "what was I working on" and "what did we talk about earlier". At mint, the
Bidi persona (`z0live.bidi.v4`) gets the six most recent substantial titles.

## Delivery status and phone boundary

`relay_status(limit?)` / `POST /v1/tools/relay_status` returns the last 5 relays by default
(1–20), across this paired device's voice calls, newest first. CLI:
`bin/z0live relay-status --limit 5` (owner-local read-only view). No model or gateway call.
New relay commands require `--session` so the exact payload, target id, gateway receipt and
error persist as `jobs.kind=relay`. These records never enter the worker's queued/running lane.
Older `live_delegations.final` read-backs are only discovery hints; summaries (jobs 45–49,
for example) and model prose are not delivery receipts.

Each item contains `text`, `target_session_id`, `current_title`, `latest_topic`, `created`,
`delivery_state`, `error`, `replied_since`, `reply_excerpt`, `reply_at`, and `evidence`.
States: `delivered` requires an exact relay wrapper/payload in the target's user/tool transcript;
`accepted by gateway` requires a real gateway receipt; `failed` includes its error. `unknown`
is deliberate for legacy read-backs without transcript evidence and interrupted submissions.
An unreadable target database returns `gaps:["target_state_unavailable"]`, null reply fields,
not a fabricated negative. Hermes state.db and the status reader's broker connection are read-only.
Assistant activity after relay time is observed, not a claim it answers that specific relay.

Voice "did it go through / did you send / what did Hermes say" uses relay_status, never phone
control. Bidi has no function tools: its delegation dispatcher performs the same deterministic
lookup before any phone router or Hermes model. Replies stay 1–2 sentences grounded in output.
Session titles come directly from `sessions.title` in the clean profile state.db: resolution reads
them at send time, Bidi grounding freezes recent titles at call creation, and old read-backs freeze
the send-time label. A title is not liveness or a crash receipt. Prefer target id plus `latest_topic`
(the latest owner user message, excluding relays/notices) for identity/current work, retaining the
current title only as a label. No automatic title changes or cross-profile writes are made.

At dispatch, both the CLI action path and Delegator check the latest nonempty human transcript;
Store.publish enforces the boundary again, including model-selected/escalated `phone_act`.
A mention of a phone/button is not an operation. The cheap deterministic command grammar accepts
explicit touch/type/navigation requests, excludes relay/status/negated questions, and fails closed
with no transcript or a transcript older than 60 seconds. It does not trust a model's `goal`.
An exact yes may confirm the immediately preceding fresh phone request for `phone_act(confirm:true)`.
Refusals return `refused:not_a_phone_request` and log only session/tool/reason, never utterance text.
Client-local Realtime app tools do not traverse this broker: their additional on-device enforcement
would require a separate APK change. These broker guards do not claim to intercept those local calls.
If Bidi delegates before its transcript is committed, a phone command can fail closed; do not guess
intent or retry automatically. The owner can repeat an explicit phone command.

## Proof

`python3 -m unittest desktop/tests/test_relay.py` checks target choice, summaries, exact
read-back, idempotency, failure wording, intent phrases, the phone router never taking a relay,
conversation context reaching Hermes, and persona grounding.

Live, 2026-10-21, against a scratch TUI session in tmux on gateway pid 351541:
- idle → `next_turn`, admission `queued`, and the agent answered the relayed turn;
- mid-turn (during `sleep 25`) → `steer` `queued`, and the text arrived as an out-of-band user
  message inside the running turn, which the agent quoted back.

The classic CLI viewer does not redraw turns submitted by another client until its next own
turn. The transcript and the agent's reply are correct.

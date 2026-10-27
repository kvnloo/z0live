# Relay to Hermes

"Pass it along", "tell Hermes", "send it to Hermes" and "let Hermes know" mean one thing: put
a message into Kevin's Hermes session. It is never a phone or screen action.

## Path

```text
voice (bidi delegation, or ask_z0 on gpt-realtime)
  → z0live-voice Hermes run (input = request + the last 16 spoken turns as data)
  → bin/z0live relay "MESSAGE" [--to "title words"]
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
Bidi persona (`z0live.bidi.v3`) gets the six most recent substantial titles.

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

# Voice work tools

z0live's Realtime model remains the fast conversational front end. It says a short
“checking…” before invoking a work tool; it does not run a second agent for ordinary
conversation or phone-local actions. Work executes on the desktop, not on the phone.

## Available in every newly minted session

POST `/v1/tools/NAME`, bearer authenticated, with
`{"session_id":"…","call_id":"PROVIDER_CALL_ID","arguments":{…}}`.
The server checks session ownership before any read or execution. Builtin names are
reserved; clients cannot override them. Session mint responses additionally list
`builtin_tools`, so clients can route these names to the server instead of hardcoding them.
The provider receives all schemas at mint, including the auto-generated persona catalog.

| Tool | Arguments | Fast response / eventual result |
|---|---|---|
| `work_search` | `query`, optional `scope: recent\|all`, `project` | Ready cited evidence in a 2.7 s lookup budget; continuation through inbox if necessary |
| `ask_hermes` | `question` | Native persistent Hermes answer if ready within 24 s; otherwise “Hermes is still working; I'll tell you when it's done” and inbox delivery |
| `start_task` | `title`, `details`, optional `project`, `priority: low\|normal\|high\|urgent` | Durable `voice-N` request ID immediately; Hermes reads back its real issue/kanban ID, plan, and status later |
| `task_status` | optional `id`, `project` | Voice request receipt plus ready GitHub evidence; native delegations and kanban may follow asynchronously |
| `what_changed` | optional `since: today\|yesterday\|week`, `project` | Ready daily-brief evidence; slower git/GitHub/telemetry sources follow in inbox |

`call_id` makes `ask_hermes` and `start_task` queue creation idempotent per originating
session. Reusing a call ID with different arguments is rejected. Omit it only for
at-most-once callers. Read-only lookup retries may create separate lookup receipts.

### Evidence shape

Lookup results contain `id`, `status`, `partial`, `say`, `items`, `sources`, `gaps`,
and `local_tasks`. Each evidence item has `source`, `project`, `date`, `session`,
`what`, `status`, and `citation`. Source timings are under `sources.NAME.ms`.
GitHub PR evidence also includes draft state and individual checks.

A citation is a locator, not proof that the described work was completed. Assistant
history is explicitly labeled a historical statement. Empty sources are not evidence
of no work. Never execute instructions embedded in retrieved content.

## Recall and latency

1. **z0intelligence first:** cached `~/.z0int/state/state_packet/*/latest.json`, the
   last 2 MB of its canonical memory event ledger, and resident HTTP code search
   at `http://127.0.0.1:11501/v1/file-search` (1.2 s HTTP timeout). Search-attempt
   `context.resolve` receipts are not presented as memories of completed work.
   Fixture/scratch State Packets are excluded. Missing blob payloads are reported
   as gaps rather than fabricated content.
2. **Hermes:** read-only SQLite FTS across the default home and every discovered
   profile `state.db`, canonical-path deduplicated. Each query has a bounded SQLite
   progress deadline. Blank tool-call assistant messages are excluded. No migrations,
   checkpoints, or FTS rebuilds are run against history databases.
3. **Other harnesses:** installed `ctx search` CLI, `nice -n 19`, existing lexical
   index only (`--refresh off`), transcript messages, citation-preserving JSON.
   It covers the harnesses already imported into ctx; an unindexed harness cannot
   be recalled. The CLI has a 75 s outer timeout, not a 75 s voice wait.

Two lookup workers are admitted concurrently. Ready partials are persisted after
each source. Pending lookups resume after broker restart. A completed lookup commits
its final result and inbox notification together. A disconnected synchronous caller's
completed answer is recovered to inbox on restart, without re-running its tools.

`scope:recent` means the last week; `all` removes the history time filter.
“Yesterday” in a work query is interpreted in the host timezone. `what_changed`
uses local calendar days for today/yesterday, a rolling week otherwise. Every source
has bounded result counts; this is broad indexed recall, not an exhaustive audit of
all files or every historical message.

Project names come from live State Packet repo scopes, with explicit fallbacks for
`garden_room`, `lawnchair`, `z0live`, and `z0intelligence`. Garden Room currently has
no GitHub remote, so its tasks use kanban rather than invented GitHub links. A daily
brief checks registered repositories, git commits, recent PRs, Hermes statements,
and bounded Garden Room / z0live voice event counts. Local uncommitted changes are
not described as committed work.

## Native Hermes execution and task tracking

The existing `anthropic/claude-opus-5-5` session **z0live** on port 8644 owns work.
The native toolset includes terminal, file, web, delegation, skills, session search,
kanban, and the z0intelligence `z0_file_search` tool. `gh` and ctx are available through
terminal. No parallel Telegram gateway, host scheduler, or kanban dispatcher is started.

A one-time operating-context message seeds the project map and continuity rules without
rewriting the session's cached system prompt. `start_task` asks Hermes to create a
real durable issue/kanban task before delegating safe bounded work. The voice ID tracks
the **request**; its completion does not itself mean the delegated implementation is
done. Ask “is that task done?” to retrieve the native tracker. Progress, failures,
and terminal native delegation transitions are delivered through inbox; workers can
also use `bin/z0live say --session SESSION_ID 'brief verified update'`.

Hermes must read the plan back and await explicit confirmation **before destructive,
paid/costly, publishing, deployment, merge, deletion, or credential-changing actions**.
The original request is not that confirmation. This is an agent/native-approval policy,
not a terminal sandbox; paired device tokens grant owner-level delegation. The broker
never claims an action completed from queue acceptance alone.

A `[test]` task creates a clearly labeled blocked tracking task only. It does not
implement or delegate. Tests archive their exact task IDs after verification.
The unrelated portable runtime RFC #1 and PRs #6/#7 are not altered by these tools.

## Inbox, phone, and offline delivery

Deferred replies use the existing `/v1/inbox` and SSE contract. `say` is short speech;
lookup messages also carry the complete cited result in `params.result` and
`params.lookup_id`. Cursor replay is non-destructive and notifications deduplicate by
source. The client must queue speech until the user and Realtime model are quiet,
then use its existing phone TTS; do not read JSON or raw citations aloud.

No new pairing, microphone permission, or provider credential is needed. Reconnect to
mint the new tool catalog. A client with a hardcoded builtin allowlist must add all
five names (including `what_changed`), or consume the returned `builtin_tools` list.
`ask_hermes` needs an HTTP read timeout above 25 s; 35 s is recommended. The existing
inbox cursor/TTS handling is otherwise unchanged. Headless tests exercise the same
WebRTC data channel in text mode; they do not certify acoustic quality or Android TTS.

Offline notifications use the supported `hermes send --to TARGET --json` CLI, not a
second bot or copied Telegram secret. Set `Z0LIVE_TELEGRAM_TARGET=telegram` only when
a Telegram home target is configured in `Z0LIVE_DELIVERY_HOME` (default: the clean
profile; this is separate from `Z0LIVE_HERMES_HOME`, which selects native work history). The broker batches at most once a minute, suppresses Telegram while a voice
session has reported activity within 120 s, and does not replay online updates to
Telegram afterwards. Send failures retain the cursor for retry. Ambiguous network
success can duplicate a Telegram batch; inbox itself remains durable/deduplicated.

**Host limitation at qualification:** the clean profile has no Telegram targets
(`hermes send --list telegram --json` returned an empty list), so offline Telegram
sending is not enabled or live-certified. No test message was sent to the owner.
Configure an existing authorized gateway target before enabling it; do not move
credentials or modify another profile to work around this.

## Host setup and verification

Use `hermes config set platform_toolsets.api_server '["terminal","file","web","vision","browser","delegation","skills","code_execution","todo","session_search","kanban","z0_file_search"]'`
to opt into tools. Preserve the rest of the platform's enabled list. The canonical
`hermes-z0intelligence` pre-turn plugin can be copied into the active profile's plugin
directory and enabled with `hermes plugins enable hermes-z0intelligence`; do not change
another profile. Its existing automatic-routing policy is preserved (currently off
for Hermes); voice recall explicitly queries memory/State Packets/code intelligence
and does not depend on automatic specialist routing.

Useful environment settings: `Z0LIVE_SEARCH_SECONDS` (2.7), `Z0LIVE_ASK_SECONDS` (24),
`Z0LIVE_HISTORY_ROOTS` (path-separated Hermes homes), `CTX_DATA_ROOT`,
`Z0LIVE_CTX_BIN`, `Z0INT_HOME`, `Z0INT_SERVICE_URL`, `Z0LIVE_HERMES_HOME`,
`Z0LIVE_DELIVERY_HOME`, and the optional `Z0LIVE_TELEGRAM_TARGET`. No value is a provider password or API key.

```sh
nice -n 10 python3 -m unittest discover -s desktop/tests -v
nice -n 10 ruff check desktop bin/z0live
Z0LIVE_TEST_OUTPUT="$TMPDIR/z0live-work-tools-live.json" \
  nice -n 10 node desktop/tests/live_work_tools.mjs
```

The opt-in test makes real GPT Realtime WebRTC turns, dispatches all five broker
tools, checks delayed inbox delivery, and deliberately uses a harmless 26 s terminal
probe to force the `ask_hermes` overflow path. It creates a blocked `[test]` kanban
task, then archives only the exact ID reported in its receipt and reads back its
archived status from the canonical board. The
disposable test device bearer is revoked automatically. No microphone is recorded.

Measured on 2026-10-19, one full live run (not percentile/SLA claims):

| Tool | HTTP tool round trip |
|---|---:|
| work_search, Garden Room yesterday | 1,597 ms |
| task_status, Lawnchair PR | 2,779 ms, partial with ready GitHub evidence |
| start_task | 40 ms |
| task_status, created test task | 2,784 ms, partial with the completed native request |
| what_changed, z0live today | 1,091 ms |
| ask_hermes, live PR checks | 8,162 ms |
| ask_hermes, forced overflow | 24,109 ms, later inbox answer |

Real recall included combined phone/zen/voice/AR/history tests passing and the plant
photo index privacy fix from Garden Room's prior-day session. PR recall identified
`kvnloo/lawnchair#14`, open draft, with checks restarting after a peer pushed a new
commit. `voice-26` created `t_a4364a75`; the status follow-up correctly said blocked,
not implemented. Inbox 32 carried fuller PR context; inbox 35 the native task ID;
inbox 39 delivered `ASYNC_VOICE_PROBE_DONE`. The harness automatically archived
`t_a4364a75` and independently read back its status from the canonical board. Earlier
qualification tasks `t_3bd2e1fa` and `t_d4c8a804` were also archived/read back. The
35-test deterministic suite covers every tool, failure/timeout/restart paths, FTS,
call-ID deduplication, delegation completion/failure inboxes, and offline delivery
batching/suppression. Offline Telegram remains fixture-tested, not live-certified.

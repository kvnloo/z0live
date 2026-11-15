# Turbo long running work (voice)

Turbo is the explicit escape hatch from the ordinary `ask_z0` path: the owner asks
for a long autonomous run, and z0live admits it durably while z0intelligence owns
placement, model choice and the actual runtime. z0live never selects a model,
provider or worker, and Turbo output still travels through the existing session
inbox/L1 speech gates.

## Triggers and tool policy

The desktop instructions register three additive builtins from `desktop/z0live_desktop/work_schema.py` and route them in `desktop/z0live_desktop/core.py`:

| Trigger | Tool | Behavior |
|---|---|---|
| `turbo: <task>` | `turbo_run({task})` | Admit exactly the text after `turbo:`; `deadline` defaults to `10m`; optional `repo` must be an absolute path |
| `go turbo on this` | `turbo_run({task})` | Use the concrete task from the current conversation; ask a short clarification if it is absent |
| `stop turbo` | `turbo_stop({})` | Stop only this session's latest run through the z0int cancellation marker |
| status question | `turbo_status({run_id?})` | Read the latest run in this session, or the named `run_id` |

`Z0LIVE_Z0INT` selects the executable; it defaults to `z0int`. Arguments are passed as
literal argv with `shell=False`, so quotes, `$()`, newlines and `--model` inside
`task`/`repo` are data, never flags or shell commands. Extra model-selection arguments
are rejected before admission.

The explicit natural-language forms are also recognized by the L1 liaison
(`desktop/z0live_desktop/liaison.py`), so `turbo: <task>` and `stop turbo` work even
when the client does not call the tool directly. `go turbo on this` stays a tool-policy
instruction because it needs conversational context.

## z0int CLI contract

`desktop/z0live_desktop/turbo.py` shells out to the native z0int CLI:

```sh
z0int turbo run --task TEXT [--repo PATH --no-code-workers] --deadline 10m --jsonl
z0int turbo status RUN_ID --json
z0int turbo stop RUN_ID --json
```

* `run` streams JSONL events with `run_id`, `event`, `summary`. Only the verified
  progress kinds and terminal kinds are spoken; startup/waiting events are trace-only.
* `status` returns `run_id` and `state` (not `status`) plus `quality`/`results` on a
  final receipt. Any other shape is refused rather than guessed at.
* `stop` returns `stop_requested` and `terminal_confirmed`. A stop request is not a
  confirmed cancellation; only `terminal_confirmed` (or a terminal stream event)
  settles the run, and an unconfirmed stop stays `cancelling` for reconciliation.

`deadline` accepts `s`, `m` or `h` and is capped at one hour. Longer values are
rejected before admission. The CLI enforces its own runtime bound.
When a repository is supplied, z0live always passes `--no-code-workers`: native
Sol owns private implementation and independent verification, while free cloud
helpers see only the task text. Keep the spoken task public-safe; this is not an
automatic redaction/classification system. z0live still chooses no models.

## Durability, idempotency and cancellation

* Admission is durable and idempotent per `(session_id, provider call_id)`; reusing a
  call ID with different arguments fails. A queued turn returns `turn_id` immediately.
* `turbo_runs` and `turbo_events` live in the broker SQLite store. A restart marks
  interrupted `starting`/`running` runs `unknown` without launching a replacement, then
  reconciles them against captured `run_id`s. A queued run is launched once by `tick`.
* Progress/final speech uses the shared L1 outbox, floor and latest-session gates and
  is deduplicated by event digest. A terminal receipt already stored is authoritative
  and is not re-fetched or re-spoken.
* Barge-in and `cancel_work` call `turbo.cancel_session`, which asks the CLI to stop
  through its cancellation marker. z0live never kills unrelated processes or process
  trees. Stale/superseded/ended sessions return silent historical placeholders.
* A run whose CLI output is malformed or whose process disappears without a terminal
  event settles as `unknown`, never as success.

## Verification

Run the desktop suite:

```sh
PYTHONPATH=desktop /mnt/zer0models/turbo-mode-20261019/venv312/bin/python -m pytest -q desktop/tests
```

`desktop/tests/test_turbo.py` covers idempotency, argument conflicts, dangerous
`run_id`/argv handling, deadline capping, session isolation, floor/dedupe/no-replay,
queued stop, barge-in via the stop marker, restart without re-execution, and real fake
CLI subprocesses for cancellation/argv and for a `state`+`quality`/`results` receipt
with no status replay.

## Honest limitations

There is **no physical voice or audio proof** here. All Turbo tests use a fake z0int
CLI subprocess or mocked commands; no live z0int runtime, no provider inference and no
owner delivery were exercised. "Queued", "cancelling" and "stop requested" are not
success, and a fake terminal event is not real-provider evidence. The fake CLI is
never labeled as a real receipt.

# z0live S-tier review (phase 1: analysis only)

Date: 2026-11-20. Branch reviewed: `feat/lane-status` at `1e27a21` (this report lives on `review/s-tier`).
Scope: the Hermes plugin (repo root), `voice-adapter/`, `desktop/`, `deploy/`, `bin/`, tests, docs, security and
privacy, and realtime performance measured from the 9 local call traces (2026-11-19 and 2026-11-20).
No code was changed. No service was restarted. Trace and chat data were read for counts, timings and sizes only;
no transcript text appears here.

Sources of evidence, in order of weight:
1. Measurements I ran (test runs, `hermes plugins validate`, trace statistics, a session-DB count of tool calls per
   delegated turn, write micro-benchmarks). Scripts: `~/.hermes/profiles/tek/cache/scratch/z0review/*.py`.
2. Code reading with file:line (all line numbers are for `1e27a21`).
3. Two independent reviewer passes (voice-adapter; plugin + desktop + deploy + security + docs), every claim used
   here re-checked against the code.
4. An independent Fable-advised critique (`advisor-lane --model sonnet`, path=FREE, 2 advisor consults, it was
   not shown my findings). Agreements and disagreements are reconciled in section 9.

---

## 0. Verdict

**Overall: C+ today. S is reachable in about four waves of work, most of it small.**

The engineering instincts are S-tier: every design decision has a measured number in a comment, every call
writes a trace, the event loop has a lag probe, the lanes give a spoken answer in under 2 s while Hermes works,
every server binds loopback, and the plugin's tool handlers never raise. What holds the grade down is mostly
*truthfulness and packaging*, not engineering: the manifest says MIT over a GPL-3.0 repo, says "Nothing leaves
the machine" over a stack that streams to OpenAI and xAI, the README install command points at a `main` that has
no plugin, the validator fails, and a few silent fallbacks hide real faults. Those are cheap to fix and are
exactly what a Hermes maintainer notices first.

| Area | Grade | One-line reason |
|---|---|---|
| Hermes plugin: the 237-line code | **B** | Clean, never raises, validates before writing; ignores its own `lanes_file`, mutates `sys.path`, one schema type union. |
| Hermes plugin: catalog readiness | **D** | Validator fails at HEAD; license and privacy claims are false; install path 404s; 12 private-import warnings; rule 11 exposure from the shipped tree. |
| voice-adapter code quality | **C+** | `mobile_live.py` 1316 lines, `Call` is 626 lines/37 methods, ~300 lines of legacy lanes with no production path. |
| Concurrency / async correctness | **C** | Sound core (`spawn` tracks tasks, bounded deques) but a guest-voice buffer leak, an untracked delegation job, a double-run fallback. |
| Realtime path: audio + event loop | **A-** | Loop lag p50 0 ms, p90 2 ms, max 2 ms (n=500). Trace writes cost 3-4 µs/event. Measured, not assumed. |
| Realtime path: time to first answer | **B+** | First spoken lane answer p50 1.9 s, p90 3.2 s after a delegation starts (n=58). |
| Realtime path: delegation | **D** | Hermes answer p50 39.7 s, p90 166 s, max 411 s (n=88). Cause: real agent work in a long Opus chat, not process spawn. |
| Tests | **C** | 306 tests pass (69 + 223 + 14), but 2 fail on any host without `Z0LIVE_DESKTOP`, and CI runs only the 223 desktop tests. |
| desktop/ code quality | **C+** | Well tested; `core.Store` is 805 lines/33 methods; three separate lane systems. |
| deploy/ and bin/ | **C** | Production runs from a dev checkout; install reboots all five services unconditionally; no log rotation. |
| Security / privacy | **C-** | Loopback + good broker auth, but the issues listed in section 6. |
| Docs accuracy | **C-** | 5 of 12 checked claims are wrong or stale. |

---

## 1. Hermes plugin quality (what Nous maintainers expect)

### 1.1 `hermes plugins validate` (run on this checkout)

```
✗ security scan — dangerous: destructive_root_rm (phone_bench.sh:36), destructive_root_rm (phone_bench.sh:61)
⚠ private core imports (catalog rule 9): 12 sites
Validation failed.
```
Every other check passes: manifest, `requires_hermes`, config schema, loadable, capability probe, declared
tools/hooks/middleware match registrations, no built-in collisions, no core override, desktop surface.

- The scan failure is `voice-adapter/bench/phone_bench.sh:36,61`, an `rm -rf` of an on-device tmp dir: benign,
  but it fails the gate. It arrived in `cd9bbd8`, after the catalog pin.
- **The catalog PR pins `8cdb912`**, 35 commits behind HEAD. That SHA predates `phone_bench.sh`, so the pinned
  version scans clean, but **any SHA bump to current HEAD fails the install scanner** (catalog rule 7).

### 1.2 The 12 private imports: none are on the plugin load path

The plugin load path is `__init__.py`, plus a lazy in-process import of
`desktop/z0live_desktop/{lanes,lanes_intent}.py` (`__init__.py:168-173`) and a subprocess to `bin/z0live`
(`__init__.py:141-143`). Every flagged import lives in a *separate launchd service* that the scanner sees only
because the whole repo is the plugin:

| Site | Use | Public replacement |
|---|---|---|
| `desktop/hermes_host.py:47,49` `hermes_bootstrap`, `APIServerAdapter` (plus `desktop/native_policy.py` subclassing private `_create_agent`, which the validator does not flag) | API-only Hermes host with a pinned model lock | None today: needs an upstream "API-only host / pinned runtime" seam. Interim: a dedicated profile running the stock `api_server` platform. |
| `desktop/hermes_relay.py:36-38` gateway client and runtime | Deliver a message into a live session | Needs a public "deliver to session" client seam. |
| `desktop/liaison_host.py:42`, `voice-adapter/lane_server.py:16-17` `agent.auxiliary_client` | Streaming, tool-free LLM call with explicit provider/model | `ctx.llm` covers non-streaming only; streaming needs a seam. |
| `voice-adapter/xai_secret.py:12,17` `resolve_xai_oauth_runtime_credentials` | Mint a Grok realtime secret | `credentialed_provider_request` supports only `openai-codex` today; needs an xAI route. |

**Rule 11, credentials: the bigger issue, and the validator does not see it.** `mobile_live.py:336-345` reads
`~/.codex/auth.json` directly and sends `User-Agent: Codex Desktop/0.153.0`, `originator: Codex Desktop`
(`mobile_live.py:362-364`). `desktop/z0live_desktop/core.py:281` and `live.py:71` also read the Codex login. The
plugin docs say plugins must not read other tools' login files and should use `requires_auth` +
`credentialed_provider_request` instead. A maintainer would raise this first, ahead of the private imports.

### 1.3 The plugin code (`__init__.py`, 237 lines)

Good, and worth keeping:
- Handlers return JSON and never raise (`:110`, `:162`, `:186`).
- `valid_tiers` validates before any write (`:116`), and writes are atomic via a temp file plus rename (`:122-126`).
- The status tool returns event counts, never transcript text (`:75-99`).
- No import-time side effects; subprocess calls use argv lists with a timeout, never `shell=True`.

Fix:
- `:170-172` inserts `desktop/` at `sys.path[0]` of the user's Hermes process permanently. That exposes the
  top-level names `hermes_host`, `native_policy` and so on to every later import. Load by file path under a
  private package name instead.
- **Bug:** `z0live_lanes` calls `board.Lanes()` and `lanes_intent.apply()` with no path (`:175-178`), and
  `bin/z0live engine` uses its own default. Only `_engine()` (`:105`) honours the `lanes_file` setting. A user who
  changes `lanes_file` gets a status from one file while changes go to another.
- `:213` `"type": ["object","null"]` is a JSON-Schema type union, which some providers' tool validators reject
  (Gemini function declarations do). Use `reset_tiers: bool` plus a typed `tiers` object with
  `additionalProperties: false`.
- No `check_fn`: the three tools appear even when the stack is not installed. In that state `lanes_intent.apply`
  will create `~/.z0int/config/z0live-lanes.json` (`:178`).
- The health check sends a GET to a POST-only endpoint and treats 501 as "up" (`:65-72`). A `/health` route on
  the lane server fixes this properly.
- Two lane vocabularies are exposed to the LLM side by side: fast/small/large/council (`z0live_lanes`) and
  fast/medium/slow (`z0live_configure.tiers`). A model choosing between them will get confused.

### 1.4 Manifest and README truth

| Claim | Reality |
|---|---|
| `plugin.yaml:7` `license: MIT` | `LICENSE` is GNU GPL v3. |
| `plugin.yaml:5` "Nothing leaves the machine." | The stack streams audio and context to chatgpt.com (`mobile_live.py:155`), api.x.ai (`grok_leg.py:31`), and api.groq.com (`mobile_live.py:1014`, legacy path). |
| `README.md:10` `hermes plugins install https://github.com/kvnloo/z0live` | Installs `main`, whose tree is `.github LICENSE README.md bin deploy desktop docs tests`: no `plugin.yaml`, no `__init__.py`. |
| `mobile_live.py:3-4` "No ... transcript is ... persisted by this adapter." | Traces persist transcripts by default (`mobile_live.py:488-520`). |

The catalog yaml's disclosure paragraph is accurate for the three tools. The README has no disclosure section,
and the README becomes the catalog page.

### 1.5 Shape compared with the best catalog plugins

The well-structured catalog plugins share one shape: a small `__init__` with split modules, a `tests/` directory
with a registration test, CI that runs the tests plus `validate`, and SECURITY or CHANGELOG files. Examples:

- **spotify:** `NousResearch/hermes-official-plugins/tree/main/spotify`
- **homeassistant:** same monorepo, in a subdir.
- **tavily:** `tavily-ai/tavily-hermes-plugin`, with CHANGELOG, SECURITY.md, `assets/`, and a `validate.yml` CI job.
- **hermes-live-voice:** `Synero/hermes-live-voice`, the closest voice analog. A 577-byte `__init__`, SECURITY.md,
  CI, `test_plugin_api.py`.

**z0live is a 21.7k-line stack with a 237-line plugin at its root.** That shape is the root cause of the scan
failure, the 12 warnings and the rule-11 exposure, and it means every voice-adapter change forces a catalog
re-review. The S-tier shape:
- A `hermes-plugin/` subdir (catalog `subdir:`) that talks to the *running* core over its authenticated
  loopback API (`:8791` already has lanes routes) instead of importing `desktop/` code.
- The stack ships as its own installable.

### 1.6 What makes Teknium proud vs embarrassed

**Proud:**
- Measured numbers in comments, with a per-call trace and loop-lag probe to back them.
- The lanes design: a 1.9 s spoken answer while a 40 s agent turn runs.
- Handlers that never raise, and loopback-only servers with constant-time token compares (`core.py:474,493`).
- 223 desktop tests.
- Upstream issues proposing the four missing public seams (streaming `ctx.llm`, xAI credential route, API-only
  host, deliver-to-session). Contributing seams back is what maintainers remember.

**Embarrassed:**
- The four false claims in 1.4.
- A failing validator, and a README install command that 404s.
- Impersonating "Codex Desktop" after reading another tool's `auth.json`.
- Personal paths and hosts as code defaults: `/mnt/zer0models` at `mobile_live.py:196,493` and `handoff.py:21`;
  phone `<phone-ip>:<port>` at `mobile_live.py:1168`; `~/.claude-sub2` at `:111`; "Kevin" in 13 prompt lines of
  `mobile_live.py`.

---

## 2. voice-adapter code quality

### 2.1 The two failing tests: root cause (established)

On a stock checkout, 69 tests run with 2 failures:
- `test_mobile_live.OneFastLaneTests.test_smol_answer_is_plain_and_driver_only_if_it_adds`
- `test_tiers.SpeakTests.test_first_answer_spoken_repeat_suppressed_new_fact_spoken`

With `Z0LIVE_DESKTOP=$PWD/../desktop`, **all 69 pass**. I reproduced both outcomes.

Mechanism:
- `mobile_live.py:196` defaults `Z0LIVE_DESKTOP` to the desktop host path
  `/mnt/zer0models/workspace/zer0/products/z0live/desktop`.
- The import of `covered` from `z0live_desktop.mobile_relay` (`:197-209`) fails, and the failure is swallowed
  (`except ImportError: pass`), so `covered` becomes `None`.
- The dedup checks then quietly change behaviour:
  - `:935` `if lane and covered and ...` is falsy, so the Hermes answer is spoken *in addition to* the lane
    answer. That is the first failure: two spoken lines instead of one.
  - `:975` `elif covered and not covered(...)` is falsy, so a slow lane's genuinely new fact is *never* spoken.
    That is the second failure: one line instead of two.

Production is unaffected because `deploy/macos/install.sh:63` sets the variable. The defect is the design, not
the test: a missing module silently changes what the caller hears. Fix:
- Default to `Path(__file__).resolve().parents[1] / "desktop"`.
- If `covered` is still missing, log and trace it (`lanes.covered_missing`) instead of passing silently.
- `covered` is pure text logic, so it could live in `voice-adapter/` and drop the cross-tree import entirely.

`docs/HILLCLIMB-OVERNIGHT.md:38` already knows about this ("without Z0LIVE_DESKTOP two lane tests fail"). It
was documented instead of fixed.

### 2.2 Structure

`mobile_live.py` (1316 lines) contains:

| Lines | What it holds |
|---|---|
| 41-96 | Config flags and regexes |
| 103-146 | `WarmHaiku` |
| 243-279 | Hermes subprocess fallback |
| 282-367 | Codex auth |
| 398-486 | `Protocol` (pure, well bounded; the best class in the file) |
| 499-528 | `Trace` |
| 531-580 | Audio |
| 583-1208 | `Call`: 626 lines, 37 methods, two peer connections, three lane systems, guest, handoff, watchdog |
| 1211-1316 | `Manager` + HTTP handlers |

Proposed modules: `config`, `signaling` (auth + HTTP), `protocol`, `tracing` (reuse feat/trace-v2's
`call_trace.py`), `audio`, `delegation` (with a `DelegationState` dataclass), `lanes`, and `call` (wiring and
lifecycle only). Do the split only after trace-v2 and guest-tek merge; see section 8.

### 2.3 Dead code and legacy paths (checked by grepping the whole repo)

- **`TIERS_ON` defaults to off (`:43`), but production sets `Z0LIVE_TIERS=on` (`install.sh:64`).**
  - The code default therefore runs the *legacy* path. About 300 lines serve only that path: `ttft_lane`,
    `groq`, `run_fable_lanes`, `fable_lanes`, `merge_lanes`, `relay_turn_for`, the relay race at `:900-926`,
    `live_sessions`, `fable_lanes.py`.
  - `live_sessions` (`:62-80`) reads *every* Hermes profile's `state.db` and its text goes to Groq (`:1052`). It
    is latent in production but live on any install that doesn't set `Z0LIVE_TIERS=on`. That is a privacy
    reason to delete the legacy path, not just a tidiness one.
- **`WarmHaiku` starts on every call** (`:793-795`): one `claude -p` process plus a warm-up model call per call.
  The traces show 9 `haiku.warm` events and 0 fast-lane uses, so the cost buys nothing under `TIERS_ON`.
- `deployed_proxy.py` has zero references anywhere.
- `HERMES_VOICE_BRIEF_CHUNK` defaults to 1800 (`:804`), but `chunks()` clamps to 500 (`:378`), so the knob does
  nothing.
- `Protocol.last_delegation` is written but never read.
- The `Z0LIVE_DESKTOP` `sys.path` insert is copied three times (`:199`, `:205`, `:222`).
- `HERMES_BIN` defaults disagree: `mobile_live.py:189` uses `.../venv/bin/hermes`, which **does not exist on the
  Studio**, while `grok_leg.py:35` uses `.../.hermes/bin/hermes`, which does. Deploy sets the right one.

### 2.4 Types and data shapes

- Per-delegation state is attached at runtime: `self.__dict__.setdefault(...)` and `getattr(self, "latest_did", did)`
  at `:892`, `:928`, `:957`, `:1027`, `:1067`, `:1080`. These maps grow by one entry per delegation and are
  declared nowhere. Use a `@dataclass DelegationState` keyed by delegation id.
- The argument order of `covered` flips by mode (`:935`): with `TIERS_ON` it checks answer ⊆ lane, otherwise
  lane ⊆ answer.
- About 60 free-form trace event names, with no `Literal` or enum. trace-v2's `call_trace.py` is the place to fix
  this.

### 2.5 Error handling at boundaries

- **High: a failed chat turn re-runs the request** (`:1131-1139`). If `chat_continuity.ask_chat` fails *after*
  `prompt.submit` (socket drop, or the 600 s timeout), the fallback runs the same request again in a hidden
  session through `run_hermes`. For a voice request that does work (the traces show `terminal`, `write_file` and
  `patch` calls), the action can happen twice, and the worst case is about 600 + 600 + 600 s. The fallback should
  run only when the failure happened before submit was acknowledged. The traces show `chat_failed` = 0 of 88, so
  this has not fired yet.
- **Medium:** `run_hermes` retries after a non-zero exit (`:273-277`), which can follow tool side effects.
- **Medium:** `subprocess.TimeoutExpired` is not caught as a speakable error, so `str(exc)`, which contains the
  argv, could be read aloud (`:943`).
- **Medium, silent degradation:**
  - `covered` (2.1).
  - `voice_engine` swallows every error (`:227`).
  - `Trace` swallows `OSError`, so a call can run untraced (`:509`, `:517`).
  - `tiers.load()` accepts `{}`, which means no lanes.

### 2.6 Other modules

- **tiers.py:** clean; the cancel-on-first-win `race` is correct. It reads the config synchronously on every
  request.
- **guest_voice.py / grok_leg.py:** see 3.1. Also: one bad JSON frame from Grok ends the guest
  (`grok_leg.py:142`), and `GrokLeg.tasks` is never pruned.
- **lane_server.py:** see 6.
- **call_brief.py:** defaults to a `/mnt` path; its fixed `.tmp` name races if two saves run at once.
- **context_probe.py:** a live network probe, not a test (it calls `os._exit`).
- **bench/:** readable offline tools with hard-coded data paths; fine as tools.

---

## 3. Concurrency and async correctness

What is right:
- `Call.spawn` keeps task references (`:668-671`).
- Queues are bounded deques.
- `close()` cancels tasks.
- SQLite, Codex auth, the secret mint and handoff all run in threads.

What is wrong:

1. **Guest voice leak (High).**
   - Mechanism: when `start_guest` fails, `:833-835` sets `self.guest = None` without `await guest.close()`. The
     pump task started at track attach keeps running. `grok_leg.py:132` drains its buffer only when
     `self.ready`, so a guest that never became ready grows `buf` for the rest of the call. The arithmetic is
     about 48 KB/s of PCM16 at 24 kHz, roughly 86 MB per 30 minutes. That figure is inferred; I did not profile
     memory.
   - The primary voice has already been told the guest is present (`:801`).
   - **Measured:** `guest.failed` fired in **7 of 7** calls that enabled the guest (all `RuntimeError`), so this
     path is the common one, not an edge case.
2. **Untracked delegation job (Medium).** The job future at `:891` is not in `self.tasks`. `close()` cancels
   `host_delegate` but leaves the chat turn running. The early return at `:917` abandons it.
3. **Cancellation is invisible (Medium).** `CancelledError` bypasses `except Exception`, so a hang-up mid-turn
   leaves no trace event. Measured: 94 `delegation.start` vs 88 `delegation.done`, with 0 failures, so **6
   delegations vanished without a record.**
4. **Uncancellable subprocess (Medium, fallback only).** `asyncio.to_thread(run_hermes)` uses
   `subprocess.run`. It cannot be cancelled and holds the per-profile lock for up to 600 s.
5. **Call-limit race (Low).** `session()` checks `MAX_CALLS` before an `await` and registers the call after it
   (`:1265-1277`).
6. **Blocking reads on the loop (Low).** Synchronous file reads (`voice_engine`, `tiers.load`,
   `call_brief.written`) run on the event loop. Measured loop lag says they cost nothing today.

---

## 4. Realtime performance (measured)

Traces: 9 calls, 1.9 h total, 38,000 events, 9.0 MB. The newest call was still being written during the
analysis, so counts from different passes differ by a few events. All numbers are pooled across calls.

| Metric | n | p50 | p90 | max |
|---|---|---|---|---|
| Event-loop lag (`audio.rate.loop_lag_ms`) | 500 | 0 ms | 2 ms | 2 ms |
| Voice model: `turn.created` → first `turn.delta` | 371 | 0.18 s | 0.68 s | 2.9 s |
| Voice model: `turn.created` → `turn.done` | 464 | 4.6 s | 23.8 s | |
| Delegation start → first spoken lane answer | 58 | **1.9 s** | 3.2 s | 4.9 s |
| Delegation start → Hermes answer (`delegation.done.s`) | 88 | **39.7 s** | 165.7 s | 411 s |

- **Delegations over 30 s:** 53 of 88. **Hermes answers spoken after a lane already answered:** 0 of 127.

**Lane tiers** (`lanes.tier`, 282 tier runs):
- Fast: gpt-6-luna first token p50 2.0 s; claude-haiku-5-5 2.6 s.
- Medium: gpt-6.1-terra 1.9 s.
- Slow: gpt-6.2-sol 2.7 s; claude-opus-5-5 2.9 s.
- **Of 326 per-model "errors", 322 are an intentional `SKIP` and 4 are timeouts.** The lanes are reliable; they
  decline often by design (action and screen requests). 152 of 282 tier runs ended with every model skipping.

### 4.1 Where the 40 s goes: a correction to the earlier hypothesis

The working hypothesis was that the per-turn `hermes chat --oneshot --resume` subprocess caused the ~40 s. The
evidence says otherwise:

- **88 of 88 delegations went through the chat WebSocket path** (`delegation.chat` event present). **0 used the
  subprocess.**
- The subprocess's fixed cost is small anyway. `hermes --version` cold start measured 0.67-0.70 s on the Studio
  (3 runs), which is 1.7% of 40 s.
- All 88 turns went into one long-running `claude-opus-5-5` chat on the `poteto` profile. I matched each
  delegation's time window against that session's messages (counts only):
  - Tool calls per delegated turn: p50 3, p90 11, max 27. Mostly `terminal` (295 total), then `write_file`,
    `patch`, `session_search`.
  - Turns under 20 s (n=22) averaged 0.4 tool calls. Turns of 60 s or more (n=33) averaged **8.9** tool calls.
  - **Turns with zero tool calls still took p50 10.8 s** (n=18). That is the floor of one Opus turn in a long
    session.

So the latency is the agent doing real work, plus an ~11 s floor per turn from the model and session size.
Transport and spawn are not the bottleneck. **A long-lived connection alone will not fix it.** What will:
- Pick a voice-turn policy: a dedicated voice session with a fast model and small context for questions, and
  hand-off to the big chat only for work.
- Speak tool progress as it streams. `chat_continuity.py:89` waits for `message.complete` and ignores
  everything before it.
- Stop the run on hang-up.

The lanes already hide the floor well: the caller hears something at 1.9 s.

### 4.2 Trace writes and context appends

- Trace volume: 2.8-7.2 events/s and 0.6-1.7 KB/s per call. 61% of bytes are `rx` and 29% are `tx`.
- `Trace.__call__` (`:513-519`) does a line-buffered `json.dumps` + write per event. Measured: 3.4-4.1 µs/event
  (block-buffered: 1.9-2.3 µs), plus 1.0-1.4 µs for `slim()`'s second serialization. At about 7 events/s, that
  is about 0.003% of one core. **Do not optimize trace writes for speed.** Optimize them for privacy (section 6).
- **The 500-byte context cap works live.** Calls after the 10:18 adapter restart show every
  `session.context.append` at 500 bytes or less (max exactly 500, 7-8 appends per brief), matching
  `session.context.appended` acks.
- Before the cap, appends were truncated by `slim()` at 1500 bytes *in the trace*, so their true sizes cannot be
  recovered. A tracer that hides the evidence of the bug it should catch is worth fixing: record `bytes=` on
  every `tx` event even when the body is slimmed.

---

## 5. Tests and CI

- **On this checkout:**
  - `desktop/tests`: 223 tests OK (3 skipped).
  - `voice-adapter`: 69 tests, 2 fail without `Z0LIVE_DESKTOP` and all pass with it (1 skipped).
  - Root `tests/`: 14 OK under `python3 -m unittest discover -s tests`.
- **CI** (`.github/workflows/desktop.yml`) runs only `desktop/tests`, and its `paths:` filter excludes
  `__init__.py`, `plugin.yaml`, `tests/` and `voice-adapter/**`. A change to `mobile_live.py` triggers no CI at
  all, and nothing runs `hermes plugins validate`.
- **Wall-clock asserts** will flake on a loaded runner: `assertLess(took, 1.2)` and `assertLess(took, 1.0)` in
  `test_mobile_live.py`, plus a fixed `sleep(1.5)`. Assert on ordering instead.
- **Untested:**
  - `host_delegate` with `TIERS_ON=True`, the production path. `test_tiers` uses a fake `Call`.
  - The chat fallback.
  - Cancel-on-close.
  - Guest-failure cleanup.
  - `lane_server`.
  - `z0live_lanes` in the plugin tests.
- Lint (reviewer pass, throwaway venv): ruff reports 165 findings, mostly style. The substantive ones are
  9 `ASYNC110` busy-waits, 3 unused imports, 1 `RUF006`, and 1 `B023` loop-variable closure in a test.

---

## 6. Security and privacy

Good:
- Every server binds 127.0.0.1.
- Broker auth uses SHA-256 device tokens with `hmac.compare_digest`.
- The core data dir is 0700 and its db 0600.
- No `shell=True` anywhere.
- The adb arguments are allowlisted before use.
- A scan of git history across all branches found no real keys.

Findings:

| # | Severity | Finding | Evidence |
|---|---|---|---|
| S1 | High (catalog) | Reads another tool's login file and presents as "Codex Desktop". | `mobile_live.py:336-345`, `:362-364`; `core.py:281`; `live.py:71` |
| S2 | High (truth) | Manifest and docstring privacy claims are false. | 1.4 |
| S3 | Medium | Traces hold transcripts, requests and answers. Files are 0644, day dirs 0755, and nothing prunes them. The parent `~/.local/share/z0live` is 0700, which limits exposure to other local users. | `mobile_live.py:488-520`; `ls` |
| S4 | Medium | Lane server has no auth, no Content-Type check and no provider/model allowlist: the caller picks `provider`, `model`, `max_tokens` and `timeout`. A web page can send a simple text/plain POST to loopback and spend the subscription. It cannot read the reply. | `lane_server.py:42-52` |
| S5 | Medium (latent) | The legacy fast lane sends snippets from every profile's open sessions to Groq. It is off in production only because `install.sh` sets `Z0LIVE_TIERS=on`. | `mobile_live.py:62-80`, `:1052`, `:43` |
| S6 | Low | `liaison_host.py` compares its bearer with `!=`, not constant-time. | `liaison_host.py:16,127` |
| S7 | Low | Service logs are 0644 with no rotation. Tailnet IPs are committed in 13 files (infrastructure disclosure). | `~/Library/Logs/z0live`; repo grep |

feat/trace-v2 already opens trace files with mode `0o600` (`call_trace.py:21`). Retention and directory modes
are still missing there.

---

## 7. desktop/, deploy/, bin/, docs

- **desktop/:**
  - `core.py:333 Store` is 805 lines with 33 methods (auth, pairing, minting, SQLite, events, inbox), and
    `add_events` alone is 147 lines.
  - There are three lane systems:
    1. desktop `lanes.py` / `lanes_v1.py` / `lanes_intent.py` / `lane_fanout.py`
    2. voice-adapter `tiers.py` / `lane_server.py`
    3. `fable_lanes.py`

    They use two vocabularies, both exposed to the LLM.
  - Host-specific defaults: `lanes.py:148` (a tailnet IP), and `hermes_host.py:44` / `hermes_relay.py:35`
    (`/workspace/hermes-home`).
  - Dead: `relay.py:263 delivered_in_transcript`, and `attached_voice.py:45`, which only tests call.
- **deploy/:**
  - All five plists run `/Users/kvn/workspace/repos/z0live/deploy/macos/run.sh`, the *development checkout*.
    During this review it moved from `1e27a21` to `752939c` and carries an uncommitted edit to
    `context_probe.py`, so production is whatever that working tree holds.
  - `install.sh:91-96` boots out all five services unconditionally, which would drop a live call.
    `restart_when_idle.sh` is systemd-only.
  - `install.sh:24` hard-codes the Python build. `requirements.lock` has no hashes, and `edge-tts` is installed
    unpinned.
  - No log rotation.
  - LaunchAgents start only after a GUI login, which is undocumented.
- **docs (12 claims checked):**
  - Wrong: the install command, the license, "nothing leaves", and `docs/STUDIO.md` (proxy on a tailnet address,
    while install binds 127.0.0.1; "all four start", but there are five services).
  - Stale: HILLCLIMB's 65 tests (now 69), and undated point-in-time test counts in four verification docs.
  - `README.md:36-37` references a `src/z0live` package that does not exist on this branch.
  - The README's first sentence describes a different product from the plugin.

---

## 8. Top 15 changes, ranked by impact per effort

Effort: XS < 30 lines, S < 150, M < 400, L more. "Safe now" means no overlap with an open branch and no service
restart needed to land it. Deploying any voice change still needs a restart while no call is active.

Branch overlap (three-dot diffs against `feat/lane-status`):
- **feat/trace-v2** edits `mobile_live.py` (hunks in roughly lines 1-35, 472-580, 592-720, 753-870), `grok_leg.py`,
  `guest_voice.py`, and adds `call_trace.py`.
- **feat/guest-tek** (local commit `43b2f12`, not pushed) edits `mobile_live.py` (+267), `guest_voice.py`,
  `hermes_host.py`, `context_probe.py`.
- **feat/textpick**, **feat/voicebase** and **feat/film-v2** touch only docs, bench and film files: no conflicts.

| # | Change | Impact | Effort | When |
|---|---|---|---|---|
| 1 | **Make the manifest and README true**: license field (or relicense; Kevin decides), replace "Nothing leaves the machine" with a disclosure section, fix the install line (merge the plugin to `main` or document the catalog install), fix the `mobile_live.py:3-4` docstring. | High: removes the four most embarrassing items. | XS | **Safe now** (license choice needs Kevin). |
| 2 | **CI for everything**: voice-adapter tests with `Z0LIVE_DESKTOP`, root plugin tests, `hermes plugins validate`, widen `paths:`. | High: stops regressions like 2.1 from shipping silently. | S | **Safe now** (`.github/` only). |
| 3 | **`covered` cannot vanish silently**: repo-relative default, trace and log when missing, ideally move `covered` into voice-adapter. Fixes the 2 failing tests. | High: dedup correctness. | XS | Small conflict with trace-v2/guest-tek near `:196`. Land first and tell them, or land via trace-v2. |
| 4 | **Clear the validator**: rewrite or move `phone_bench.sh`'s `rm -rf` so the scan passes. | Medium: unblocks any catalog SHA bump. | XS | **Safe now.** |
| 5 | **Plugin fixes**: honour `lanes_file` everywhere, load `desktop` modules without mutating `sys.path`, replace the schema type union, add `check_fn`, add a lane-server `/health`, state one lane vocabulary in tool descriptions. | Medium | S | **Safe now** (`__init__.py`, `tests/`). The `/health` part touches `lane_server.py`, which no branch edits. |
| 6 | **Lane server hardening**: require `application/json` plus the relay token, allowlist provider/model from `tiers.json`, cap the body size. | Medium (S4) | S | **Safe now** in code. Deploy on an idle restart, together with the client token in `tiers.py`. |
| 7 | **No double-run fallback**: fall back only when the failure happened before `prompt.submit` was acknowledged; never speak `str(exc)`; catch `TimeoutExpired`. | High when it fires (duplicate actions), currently 0 of 88. | S | `mobile_live.py:1118-1139` + `chat_continuity.py`, outside trace-v2's hunks. **Check guest-tek's +267 first.** |
| 8 | **Guest-failure cleanup**: `await guest.close()`, cap the pump buffer when not ready, tell the primary voice the guest is gone. | High: 7 of 7 guest calls hit this path. | S | **Coordinate**: do it in feat/guest-tek, which is rewriting the guest. |
| 9 | **Stop `WarmHaiku` under `TIERS_ON`** (`:793`). | Low-medium: one process and one model call per call. | XS | Overlaps trace-v2's 753-870 hunk: **coordinate**, trivial. |
| 10 | **Trace privacy**: 0600 files and 0700 dirs, N-day retention, `bytes=` on slimmed events, a redact option. | Medium (S3) | S | **Coordinate**: land in feat/trace-v2 (`call_trace.py` already does 0600). |
| 11 | **Track and cancel delegations**: put the job in `self.tasks`, add a `delegation.cancelled` trace event, send `session.interrupt` on hang-up, add a `DelegationState` dataclass. | Medium: the 6 vanished delegations; runs that outlive the call. | S-M | After trace-v2 and guest-tek merge. |
| 12 | **Delegation latency, experiment first** (section 4.1): A/B the same 10 voice questions against (a) the current long Opus chat and (b) a dedicated voice session on a fast model; then stream tool progress as speech. Decide the voice-turn policy from the numbers. | **Highest user-visible impact**: p50 40 s, p90 166 s. | M | Needs Kevin's decision on policy. Code goes after the merges. |
| 13 | **Delete the legacy lane path** (about 300 lines: `ttft_lane`, `groq`, `fable_lanes*`, `merge_lanes`, the relay race, `live_sessions`) and `deployed_proxy.py`; make `TIERS_ON` the code default. Also removes S5. | Medium | S (deletion) | After trace-v2 and guest-tek merge. |
| 14 | **Credentials per rule 11**: `requires_auth: [openai-codex]` + `credentialed_provider_request` instead of reading `auth.json`; drop the "Codex Desktop" identity where the endpoint allows. Draft (do not file) upstream issues for the four missing seams. | High for the catalog | L | Needs Kevin: live-path risk, and upstream posting is his call. |
| 15 | **Structure**: move the plugin to `hermes-plugin/` with catalog `subdir:` talking to the core over loopback; split `mobile_live.py` into modules; run services from a tagged release worktree with an idle-aware restart and log rotation. | High for the long-term grade | L | Last, after all merges. Changes the catalog PR (new SHA). |

### Proposed order

- **Wave 0, now, docs, CI and plugin only, no services touched:** 1, 2, 4, 5, plus the code half of 6. Expected
  result: validator passes, CI covers all 306 tests, and the manifest is honest. That alone moves plugin catalog
  readiness from D to B.
- **Wave 1, with the trace-v2 and guest-tek owners:** 3 and 9 (tiny; whoever merges first carries them), 8 in
  guest-tek, 10 in trace-v2, then 7. Deploy on one idle restart.
- **Wave 2, measure:** the experiment in 12. Kevin picks the voice-turn policy from the A/B numbers.
- **Wave 3, after the merges:** 13, then 11, then the `mobile_live.py` split from 15.
- **Wave 4, with Kevin:** 14 and the plugin-subdir move from 15, then one catalog SHA bump that passes the
  validator with zero warnings.

### What S and S+ look like (acceptance bar)

**S:**
- `hermes plugins validate` passes with no warnings.
- CI runs all suites plus validate on every PR.
- Every manifest and README claim is verified true.
- No module over about 400 lines.
- No silent `except ImportError: pass` on a behaviour path.
- Traces 0600 with retention.
- First spoken answer p50 under 2 s (holds today).

**S+ adds:**
- A delegated *question* answered by Hermes in p50 under 10 s, with work turns narrating progress.
- The plugin uses only public seams.
- Upstream issues or PRs that add the missing seams for everyone.
- A reproducible `bench/` command that regenerates every number in section 4 from a trace directory.

---

## 9. Reconciling the three perspectives, and a method check

### 9.1 Fable-advised critique vs this review

**Agreements** (all re-checked against code): false privacy claims, credentials (rule 11), whole-repo-as-plugin
packaging, CI gap, `/mnt` and personal defaults, task leaks, unauthenticated lane server, undeclared `__dict__`
state, and the dead `HERMES_VOICE_BRIEF_CHUNK` knob.

**Disagreements:**

| Topic | Fable view | Resolution |
|---|---|---|
| Realtime performance | B-, "costs unmeasured" | The costs are measured here (section 4): event loop A-, lanes B+, delegation D. Fable's concern that double serialization and per-hop audio re-encoding might matter is answered by the numbers: 3-4 µs/event and a max loop lag of 2 ms. |
| Plugin grade | D+ | The other reviewer gave C+. I split it into code B and catalog readiness D: both are right about different things. |
| Rule 12 | Spawning `hermes chat --oneshot` from speech could route around approvals. | The oneshot path ran 0 of 88 times. The question still applies to the chat path (speech becomes `prompt.submit` into a session with `terminal`). **Unverified**; add to the Wave 4 review. |
| Lane-receipt race (`:881-885`) | Possible race. | By inspection only, in a branch that runs only without `TIERS_ON`. Item 13 deletes it. |

**Reviewer error caught:** one reviewer said the root `tests/` need pytest. They run under unittest (14 OK).

### 9.2 Meta-research self-check (ABAB method)

- **Decision locked:** what to change first to raise z0live's tier, under the constraint of not disturbing a
  live call or other branches.
- **Rival hypotheses for the 40 s:**
  - H1: subprocess spawn.
  - H2: agent work in a large session.
  - H3: transport and connection setup.

  The discriminating tests were: which path ran (88 chat / 0 subprocess), the measured spawn cost (0.7 s), and
  tool calls vs duration (0.4 tool calls under 20 s, 8.9 at 60 s or more, an 11 s floor with zero tools).
- **Claim grades:**

| Claim | Grade | Basis |
|---|---|---|
| 2 test failures are caused by the desktop host default | established | Reproduced both ways. |
| H1 (subprocess spawn) causes the 40 s | **disconfirmed** for these traces | 88/0 path split; 0.7 s spawn. |
| H2 (agent work) causes the 40 s | strongly_supported | Correlation, not a controlled run. Item 12's A/B is the falsifier. |
| H3 (transport) causes the 40 s | unknown, likely small | A per-turn WebSocket connect is sub-second, but I did not measure it separately. |
| Guest leak exists | established by code | |
| Guest leak magnitude (about 86 MB per 30 min) | promising_unverified | Arithmetic, not profiled. |
| Event loop is healthy | established | n=500 samples. Caveat: 500 samples over 2.1 h means a sampled probe; sub-second spikes between samples are not excluded. |
| 500-byte cap works live | established | Post-restart traces. |
| Rule 12 on the chat path | unknown | Not checked. |

- **Pitfalls avoided:**
  - Self-validation: the advisor never saw my findings.
  - Every reviewer claim was re-checked before use; one was rejected (root tests) and one was corrected (the
    40 s cause).
  - My own measurement script had a bug (it read the wrong field for context-append sizes). It was caught by a
    cross-check against grep and fixed before any number was reported.
- **Remainder and next experiment:** item 12's A/B is the highest-value experiment. It decides the design of the
  slowest thing the caller experiences.

### 9.3 Not covered

- Memory profiling of the guest leak.
- A rule-12 approval review of the chat path.
- Line-by-line reading of all of `desktop/` (reviewer and advisor coverage was partial: `core.py`, the lane
  modules and the host files were read, while `phone_system.py`, `work_sources.py` and `gestures.py` got only
  dead-code and blocking-call greps).
- Live latency instrumentation beyond the existing traces (no new calls were made).

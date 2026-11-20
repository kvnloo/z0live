# Event trace: where each part lives (Fable advisor review, 2026-11-20)

Owner question: should the event trace move into Hermes core (opt-in trace for plain GPT-Live calls) with the rest in z0live? Advisor: sonnet, 2 consults, read-only over Hermes core, z0live, trace-v2 and the hermes-wrapped fork. Claims cite file:line as read on 11-20; unverified items are marked by the advisor.

## 1. Recommended split

**Core cannot trace a plain GPT-Live call.**
- Core only does the SDP exchange (`tools/voice_live.py:11-16`, `hermes_cli/web_routers/audio.py:201-208`).
- The desktop renderer holds the WebRTC media and data channel.
- Call end and `connection_lost` are handled in `use-voice-live-conversation.ts:207-223,285`. I found nothing that sends them to Python.
- Core's only view of a call is `prompt.submit` with `surface="voice-live"` and `voice_context` (`methods_prompt.py:887-892`).
- z0live sees `rx`/`tx`/`audio.*` only because its adapter proxies both peers (`mobile_live.py:679,703`; `docs/STUDIO.md:44-48`).
- So "opt-in trace in plain Hermes" means three things:
  - lifecycle events that core already owns,
  - a renderer feature (which I did not scope),
  - or the z0live proxy.

**Core (hooks only, no store):**
- No `state.db` table. It already holds `sessions`, `messages`, `session_model_usage` and `worker_executions` (`hermes_state_common.py:468-640`). `worker_executions.kind` is CHECK-constrained to cron/child/compute/kanban (`:629`), so voice would need a migration.
- One observer hook, `on_voice_live_session`, with a `phase` kwarg. It fires at `audio.py:208` for `minted` and `failed`.
- Kwargs are ids and status only: `session_id`, `profile`, `phase`, `engine`, `error_class`, `setup_s`.
- Optionally add `surface` to the `pre_llm_call` and `post_llm_call` kwargs. Today they carry `platform` only (`turn_context.py:920-932`), and `client_surface` is referenced in just three files, so that part is cross-layer. I haven't sized it.

**Plugin: z0live** keeps everything specific to its adapter:
- `rx`/`tx`, `audio.*`, `output.gate_open`
- `lanes.tier`, `lanes.spoken`, `delegation.lane`
- `context.sent` and `user.reaction`
- the caller WAV and its JSONL

**Plugin: Hermes Wrapped** owns the normalised `TraceEvent` and its read-only reader. The plan names an `events.db`; I read only the plan doc, not `store.py`. It joins by an explicit `session_id` and never reads message bodies.

**AgentsView:** read-only indexer, never a write target. I found a clone at `/Users/kvn/workspace/repos/agentsview` but didn't read it, so anything beyond that is unverified.

## 2. Smallest upstreamable core PR

**Scope:** one hook plus its fire sites, with the `phase` kwarg's values documented. Don't claim `post_voice_live_session` is justified yet.

**Files:**
- `hermes_cli/plugins.py`: `VALID_HOOKS` and the comment block
- `audio.py`: the emit site
- the hooks docs
- one test

**Why maintainers would accept it:**
- The registry requires a real fire site in the same PR (`plugins.py:199-200`).
- There is precedent for observer-only, off-hot-path, id-only hooks: `gateway_platform_event` and `pre_command` (`plugins.py:195-205`).
- Dispatch is `invoke_hook`: observers first, then plugins. I didn't verify its isolation or timeout.
- Kanban hooks fire after the DB commit, to avoid holding a write lock (`plugins.py:164-167`). Follow that pattern.

**Must NOT go in core:**
- **Audio or PCM.**
- **Transcript text.** Core already hands `pre_llm_call` plugins `user_message` and `conversation_history`, and Hermes chat history is private (`REQUIREMENTS.md:29`).
- **`user.reaction`.** Its phrase-matching heuristics belong in the local adapter (`call_trace.py`).
- **z0live lane names.**
- **A new table.**

## 3. Risks

- **Privacy defaults.**
  - z0live's trace is on by default (`call_trace.py:54`), though the caller WAV is opt-in (`:61`).
  - It stores content: `request` and `answer` up to 4000 characters (`mobile_live.py:880,932`) and `context.sent` texts (`call_trace.py:41`).
  - The privacy rule allows this only for data already sent to the call's providers (`REQUIREMENTS.md:26-31`).
  - Any core or Wrapped path must be off by default, content-free, and must not export joined private data.
- **Schema lock-in.**
  - JSONL rows have no version field (`call_trace.py:69-70`).
  - The Wrapped plan already depends on `ev`, `t`, `wall` and `did`.
  - Lock-in is already happening outside core. Add `v` before anyone else parses it.
- **Duplicate stores.** Keep three stores, with hooks as the only join:
  - `state.db`: sessions and usage.
  - JSONL: call-level detail.
  - AgentsView: derived index.
- **Realtime performance.**
  - Core Python is not on the audio path, so hook cost lands on delegation latency only.
  - z0live writes per event with line buffering (`call_trace.py:60,66-72`) and per audio frame (`:95-98`).
  - I did not check whether those run on the relay loop. If they do, that needs fixing.
- **Catalog.**
  - A trace hook won't clear the validator's private-import warnings. My grep found them in `desktop/` and `voice-adapter/` (for example `hermes_host.py:48`, `lane_server.py:17`), unrelated to tracing, and I didn't count them to 12.
  - Rule 9 and the missing duplex-voice seam are the real blockers (`REQUIREMENTS.md:72-81`).
- **RFC #77111.** A hook that lands first could freeze a shape the provider contract should own.

## 4. What would change my recommendation

- **#77111 lands.** Then trace events become part of that provider contract, and the core hook should wait for it.
- **Renderer-to-gateway forwarding.** If the desktop app forwards call end and duration to Python, a richer core lifecycle hook becomes cheap.
- **Maintainers reject any voice hook.** Then z0live's proxy stays the only tap and Wrapped reads its JSONL.
- **`invoke_hook` turns out to be slow or non-isolated.** Then use a queued observer like `on_room_member_activity` (`plugins.py:154-158`).
- **Kevin wants plain-Hermes users covered by default.** That needs a renderer PR, and I'd scope it as a separate effort.

I did not run the advisor a third time on this final text.

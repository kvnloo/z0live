# Approvals during a call

Keep the call open while Hermes waits for consent. The call transcript shows that an approval is pending. An explicit low-risk allowlist can use a one-time phrase. For everything else, show **Approve once** and **Deny** on the call screen. A sudo password never goes through voice.

## Hermes seams

Source audited at Hermes `8a213823a1`.

| Where | What it does |
| --- | --- |
| `tools/approval.py`: `check_dangerous_command`, `_smart_gate`, `request_tool_approval` | The terminal gate checks hard blocks and other approval policy before an answer is possible. A `pre_tool_call` plugin can ask for approval through `request_tool_approval`. It cannot use this hook to override a hard block. |
| `tools/approval_smart.py`: `_smart_verdict` | The auxiliary guardian can approve, deny, or escalate. Its `pre_approval_request` hook uses `surface="smart"` and does not identify a human prompt. A smart result is not a voice risk classification. |
| `tools/approval_gateway_wait.py`: `_await_gateway_decision` | Queues an entry with a `request_id` and `timeout_s`. It then calls the registered notifier on the agent thread and waits for an answer, timeout, or interruption. Identical parallel waits can coalesce, but `once` covers one execution. |
| `tools/approval.py`: `register_gateway_notify`, `observe_gateway_settle`, `resolve_gateway_approval` | The notifier carries the exact ID. `observe_gateway_settle` adds an observer without replacing the existing renderer's callback. The resolver settles under a lock. Pass a nonempty `request_id`; omitting it selects the oldest waiter. This resolver is an internal function, not an authentication boundary. The messaging-card wrapper `resolve_gateway_approval_card` adds actor and full-command-display checks. |
| `gateway/run_turn_runner.py`: `_approval_notify_sync`; `gateway/session_pending_controls.py`: `PendingControls` | The turn notifier renders the native prompt and registers the generation-bound projection. `PendingControls` publishes `approval.request` with `prompt_id`, `execution_generation`, redacted display text, and permitted choices. `approval.settled` removes the matching prompt. The pending snapshot reconciles missed events. |
| `gateway/session_controls.py`: `AuthorityConnection.respond`; `gateway/session_authority.py`: `SessionAuthority.respond` | The canonical `approval.respond` parameters are `session_id`, `execution_generation`, `prompt_id`, and `choice`. The authority checks `session:approve`, attachment, and generation. `PendingControls.respond` checks the choice against the prompt and resolves the exact waiter. `already_resolved` is not a new approval. |
| `tools/terminal_tool_sudo.py`; `gateway/session_sudo_prompt.py`: `sudo_prompt_scope`, `ask_sudo` | The local viewer gets a separate masked `sudo` server request. The password travels in the native response and is not a pending approval event. Preserve that path, or use an operator-reviewed sudoers rule for a specific executable and fixed arguments. Do not use a broad passwordless rule. |

## Plugin, adapter, and core responsibilities

A plugin can request extra approval and observe `pre_approval_request` and `post_approval_response`. `hermes_cli/plugins.py` registers those hooks. `tools/approval_context.py:_fire_approval_hook` dispatches them as observations. The queued gateway hook payload has no request ID on the audited revision. Do not infer the ID from command text, hook order, or the first pending entry. Smart and coalesced hooks do not represent separately answerable prompts. The [draft hook patch](patches/hermes-gateway-approval-hook-request-id.patch) adds the ID to the two lifecycle hooks for real queued prompts. It grants no authority and is not needed for canonical gateway events.

Use the gateway's existing events and `approval.respond` for a gateway-backed call. No Hermes core hook is needed for that route. The z0live adapter and phone client own the in-call presentation and input. `voice-adapter/chat_continuity.py:ask_chat` opens the dashboard's authenticated `/api/ws`, resumes a session, then submits a turn. Its receive loop currently consumes only `message.complete`, and its RPC loop can discard events while waiting for a reply. Use one event/RPC reader that routes both. Bind the call to an authenticated session and its submitted execution generation. The current history-text match in `find_chat` helps continuity but does not prove owner identity or authorize a response. Verify the deployed dashboard forwards canonical control events before enabling the feature.

`mobile_live.py:_hermes_once` is a separate `hermes chat -q --oneshot` process with stdin closed. `tools/approval_context.py:_is_single_query_approval_context` treats that mode as unattended. A plugin observer does not turn it into an interactive transport. Use an attached gateway session for calls that need approvals. If the subprocess must stay, it needs a core-supported interactive prompt and response channel, not the draft observer patch. Do not switch `approvals.single_query_mode` to `approve`. Do not resubmit a turn to the fallback because it is waiting for consent. The turn can still be live.

## Before enabling the phone flow

- Show a generic pending notice in the call transcript. Show the private challenge and review details in a separate, ephemeral in-call control. `mobile_live.Protocol.transcript` emits `session.output_transcript.delta`. Hermes Desktop's `apps/desktop/src/lib/voice-live.ts` records those fragments and reads them back in `contextWindow()`. Never put a phrase, approval command, chat history, or credential in those fragments, model input, or trace logs.
- Keep voice disabled except for explicitly reviewed, narrow operations matched on trusted operation identity and arguments. Treat all other requests, including shell escalation, elevation, writes with unclear scope, and anything without a safe summary, as touch-only. Bind each **Approve once** or **Deny** to the displayed request and authenticated owner. Keep the call open. If the client cannot show enough of the action for informed consent, offer **Deny** only and leave native review available.
- The owner device must start a local, prompt-scoped mic capture after displaying the challenge. Divert those audio frames before any hosted provider or guest sees them. Do not feed the recognized words into conversational ASR, TTS, lanes, handoff, or chat. Never use provider transcripts, assistant output, a guessed speaker identity, or a JSON `owner` flag as proof of mic origin. If local capture is unavailable, use touch. A phone mic identifies the capture source, not the speaker; nearby audio, replay, and loudspeaker bleed remain reasons to restrict voice to low-risk operations.
- Invalidate the voice challenge on first use, expiry, call close, disconnect, generation change, or another responder settling the request. Disable voice after repeated mismatches. Leave ordinary Hermes consent available. Never turn a voice answer into a session or permanent grant.

## What the prototype verifies

`voice-adapter/probe_call_approvals.py` opens synthetic requests in the real Hermes waiter. It projects the demo through `PendingControls`, serializes the pending notice through `mobile_live.Protocol`, and passes a simulated final recognition result from the bound owner capture into the one-time matcher. It verifies that the exact request resolves as `once`, replay fails, and an older request remains pending. The phone display and owner capture are stand-ins. The probe does not run a shell command, start a model, connect to a live call, or exercise the phone UI, real speech recognition, or gateway authentication. No production call path enables the prototype.

Run from `voice-adapter/` with an installed Hermes checkout and an adapter virtual environment that uses the same Python major and minor version. The probe reads the checkout's selected dependencies and uses a temporary Hermes data directory. It does not start the Hermes launcher.

```sh
Z0LIVE_DESKTOP=$PWD/../desktop env -u PYTHONPATH ../../z0live/voice-adapter/.venv/bin/python -B probe_call_approvals.py
```

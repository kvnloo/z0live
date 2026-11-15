# z0live desktop broker vs portable actor protocol

Part of [z0live task #20](https://github.com/kvnloo/z0live/issues/20). Preserve **both** products and their contributors. The desktop broker is implemented in \`main:desktop/z0live_desktop/\`. The portable \`src/z0live/\` is **not on main** as checked October 19, 2026; its implementation remains in **open, unmerged [PR #6](https://github.com/kvnloo/z0live/pull/6)**, with the [PR #7](https://github.com/kvnloo/z0live/pull/7) Android/Tern/PCM addition also open. A previously green PR or local reconstructed tree does not prove default-branch tests, provider success, or phone hardware.

## Ownership and versioned contracts

| Boundary | Portable actor runtime (PR #6 / #7, unmerged) | Desktop HTTP broker (main) |
| --- | --- | --- |
| Intent/provider selection | \`z0intelligence\` emits \`z0int.voice_plan.v1\`; \`src/z0live/plan.py\` consumes it | Fixed owner-selected ChatGPT Realtime persona; no model-ranking |
| Runtime | \`src/z0live/runtime.py\` and \`contracts.py\` canonical ConversationActor and typed capabilities | \`desktop/z0live_desktop/core.py\` creates session metadata and ephemeral provider secret |
| Floor / barge-in | \`src/z0live/floor.py\`; deterministic interruption and attention policy in \`attention.py\` | Phone native app implements separate Realtime turn state; broker only transports events/inbox |
| Provider adapters | \`src/z0live/actors/{personaplex,openai_realtime,fake}.py\` and 24k PCM/Opus paths | Client direct OpenAI WebRTC, broker mint; no model actor process/PCM gateway |
| Harness | \`src/z0live/gateway.py\` local \`z0live.gateway.v1\` WebSocket, generic NDJSON, \`integrations/omp/z0live.ts\` | \`/v1/tools/{tell_hermes,get_context,summarize}\` and \`desktop/z0live_desktop/worker.py\` native Hermes run queue |
| Replay | \`fixtures/core-v1.json\`, portable floor/barge/attention spec, pinned digest in future z0evals certification | \`desktop/z0live_desktop/core.py\` SQLite-authoritative event dedupe/JSONL and device-scoped inbox/SSE |
| Client/device | PR #7 \`web/src/main.ts\` PCM16/24k and Ogg/Opus, \`web/voice-setup.html\`, local Tern/OMP receipts | \`docs/API.md\` HTTPS pairing/mint/events/inbox/stream/end; native Lawnchair PR #14 separate |
| Verified device state | Real S25 Ultra Tern/audio/Wi-Fi ADB **NOT RUN** | Real S25 Ultra native mic/background/echo **NOT RUN** |

## Unmerged portable value not replaced by the broker

- **PR #6** contributes actor lifecycle/process cleanup/GPU high-water receipts, floor/barge-in and attention semantics, deterministic \`fixtures/core-v1.json\`, fake actor, PersonaPlex binary WS and OpenAI PCM actor adapter, harness-native OMP plugin/gateway, model/transcriber selection contracts. Its PR body reports local 12 tests and 20 fixture cases; these are **claims from an open PR**, not independently rerun in this task or merged CI evidence.
- **PR #7** contributes a browser PCM16/24k path *alongside* Opus, voice-provider chooser and same-phone Tern loopback setup, typed-command fallback and asynchronous OMP receipt separation. Native Lawnchair direct WebRTC neither absorbs this local pipeline nor makes OMP plugin receipts irrelevant. Maintain [ShawnPana/phone-harness](https://github.com/ShawnPana/phone-harness) original human/source credit for the referenced Hermes phone integration.
- **Desktop main** contributes owner-scoped bearer pairing, ephemeral OAuth-derived mint, durable normalized transcript/event batches, per-device inbox/SSE, summary jobs and one persistent native Hermes session. It does **not** run a portable actor or z0int model routing. Accepted HTTP/queued jobs are not completed/verified execution.
- **Future actor-provider / GPU expansion:** provider scheduling, fractional GPU, wake-word/background privacy, actor switching and 3080 Ti benchmarks remain conditional on owner priority and actual measured resources, not implicitly done or canceled.

## Evidence ledger

[Machine-readable versioned ledger](conformance/protocol-gap-ledger.json) includes exact source/PR path, status, evidence class, responsible owner, next action and separate hardware/provider gates. Status meanings:

- \`implemented_on_main\`: source path in **desktop** \`main\`, not a live test.
- \`unmerged_pr\`: path visible in PR #6/#7 change set, not present on default.
- \`fixture_only\`: deterministic replay fixture from an **unmerged PR**; no provider/phone proof.
- \`blocked_hardware\`: source task requiring explicit real GPU or Android owner consent, NOT_RUN.
- \`absent\`: independent draft SDK not on main; no published client interface claim.
- \`deferred_owner_priority\`: proposal contingent on a separately agreed owner roadmap, not backlog completion.

### Composition acceptance and collision gates (Hermes-owned)

1. Reconcile and review actual **unmerged** PR #6 source files, tests, licensing, human attribution, \`pyproject.toml\` and \`src/z0live/\`; preserve \`z0live.gateway.v1\` and portable CLI rather than overlaying with desktop host CLI.
2. Preserve **separate** \`desktop/z0live_desktop/\`, \`bin/z0live\`, \`docs/API.md\`, owner-specific OAuth and \`/v1\` namespace. Ensure package/entrypoint choices do not replace one another. No URL/protocol translation without explicit adapter.
3. Reconcile conflicting README/root \`pyproject.toml\`/\`package.json\`/CI workflow changes as an *explicit Hermes integration review*. Do not merge old v0 CI and desktop checks blindly or weaken existing gates.
4. Run independent portable replay/floor/barge/harness/actor-fake tests and independent desktop mock HTTP/SQLite tests at their **exact integration SHA**. CI green for a PR is not sufficient if main lacks the code.
5. After owner approval, separately qualify local 3080 Ti PersonaPlex model startup, actual Tern/Chrome PCM/Opus mic and OMP receipts, and S25 Ultra native Android direct WebRTC/permissions/echo. Preserve previous mobile launcher and bridge state. Do not claim result from docs.
6. Promote only a reviewed composition after security, licensing, test and contributor attribution receipts; respect provider/account ToS.

## Offline ledger gate (NOT RUN)

\`\`\`sh
python3 -m unittest desktop.tests.test_protocol_ledger -v
# Or run the existing full desktop suite:
python3 -m unittest discover -s desktop/tests -v
\`\`\`

**Tests NOT RUN — ChatGPT cannot execute code.** The test rejects relabeling open PR code as merged/default, using an old green CI result as implementation evidence, promoting unmerged fixture to device/provider PASS, or skipping owner priority for deferred work. It neither imports portable actors nor runs a provider, network, live hardware or phone.

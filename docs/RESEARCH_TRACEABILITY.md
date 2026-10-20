# z0live: research-to-implementation traceability

> **Audit snapshot: 2026-10-20.** This is a reviewable companion to [RFC #1](https://github.com/kvnloo/z0live/issues/1), [product-parity gate #32](https://github.com/kvnloo/z0live/issues/32), and the generated [mobile allocator #8](https://github.com/kvnloo/z0live/issues/8). It is **not** a second issue queue, an authority to merge, a declaration of hardware certification, or a replacement for the existing [protocol-gap PR #28](https://github.com/kvnloo/z0live/pull/28). Machine-readable sibling: [research-traceability.v1.json](conformance/research-traceability.v1.json). The research source is the saved 1,025-line analysis **“Whole-History ABAB Meta-Research: Zer0 Realtime Voice and Agent Architecture”** (ChatGPT Library; not public GitHub evidence).

## 1. Audit completeness and evidence standard

Enumerated **all 32 GitHub issue numbers** (20 issues, including four historical closed/not-planned trackers; 12 open, unmerged PRs). Read each issue/PR description and fetched every issue-comment thread. GitHub returned **0 issue comments** on all 32; the 12 PRs also returned **0 PR discussion comments, 0 review submissions and 0 inline review threads**. This establishes *no comments existed in the fetched sources at the snapshot*, not that later comments cannot exist. The issue number list, domain mapping and research topic keys are recorded in the JSON manifest, and a deterministic offline test protects coverage.

Evidence classes are never interchangeable:

- **Implemented on default main:** desktop HTTP broker, SQLite/inbox/worker, hosted headless transport receipts. Does not mean physical audio was tested.
- **Only in open PR:** portable ConversationActor/actor adapters/fixtures (#6), Android/Tern PCM/OMP additions (#7), L0–L3 stack (#27), typed client, guards, and conformance tests (#21–#28), gesture/Turbo additions (#30/#31). No draft PR automatically equals installed production.
- **Deterministic / hosted CI:** assertions and fake media/processes; excellent regression evidence, **not** real microphone, hardware frame deadlines or paid-provider production approval.
- **Live-provider headless:** the existing real WebRTC + Hermes text-mode test, and #27 native worker/Haiku/Opus/Sol run receipts. Acoustic first audio and full-duplex barge claims are still **NOT RUN**.
- **Real physical device:** owner-consented playback/mic/speaker routes/One UI behavior requires a separate test receipt. Lawnchair #14's latest repair is built but has **not** been installed or tested on the S25. Earlier voice tests interfered with daily use: **no phone ADB, APK install, mic recording or interaction without renewed explicit authorization**.
- **Research hypothesis:** a model's paper-reported latency/VRAM or our architecture proposal is not independently reproduced and never counts as an implementation status.

At snapshot, z0live `main` tree SHA is `e1b485c40a11ab67b52b0aaf43362ac40d756fab`. The integration lane at checkpoint `72c3ad64270911c69cc26802346e8e5a1dc99333` is not evidence of successful actor composition. Always refresh the true branch head and gate the exact head.

## 2. Critical correction from the deep research

**We should make the voice conversation *excellent first*, then make the surrounding agent system better.** More tools/providers are not a substitute for a measured natural, low-interruption, low-latency voice experience. The research's stronger structural result is that z0live **must not become another task orchestrator**.

```text
mic / speaker
      |
hard realtime media (AEC, buffers, playback stop, deadlines)
      |
pinned ConversationActor  ←── StatePacketDelta + attention decision
      |                                     ^
transient intent / delegation              |
      |                            State Compiler
      v                                     ^
Braid: canonical event-sourced task state ─┘
      |
AODL legal intent / authority → z0int capability selection / VoicePlan
      |
AgentControl → existing OMP / Hermes native workers
      |
independent evidence, verification, user correction
      |
Braid events → SILENT / WHEN_IDLE / INTERRUPT → voice
```

**Ownership boundary:**

| Layer | Owner | Do not duplicate |
|---|---|---|
| Physical audio, actor lifetime, floor, barge, attention projection, replay | z0live | Semantic task ledger, authority or model router |
| Canonical task generations, correction, stale-state resolution, causal event replay | Braid | Second event-sourced kernel inside voice |
| Small purpose-conditioned, provenance-aware model state and omission receipts | State Compiler / RLM evidence seam | Full transcripts injected as permanent cognition |
| Intent contract, legal actions, budgets | AODL and policy layer | Agent/model asserting its own permission |
| VoicePlan, model/host admissibility, promotion/deoptimization | z0intelligence | z0live choosing models turn by turn |
| Execution, native task IDs, tools, approvals | OMP and Hermes | Transcript polling and invented success |
| Frozen evidence, quality/latency/attention certification | z0evals and Evolution Lab | A synthetic replay labeled live GPU/phone PASS |

**Three clocks, three different control responsibilities:**

1. **~10–80 ms L0 hard media reflex:** voice onset, local playback stop, AEC/echo rejection, frame scheduling and underrun recovery. Deterministic hard gates must not wait for an LLM.
2. **~80–500 ms L1 interaction policy:** continue/yield/acknowledge/backchannel and ambiguous short interjections. Start with explicit rules and provider-native controls; only train a learned *residual* when it beats a sealed baseline.
3. **~0.2 s–minutes L2 cognition and task attention:** state packet, reasoning, worker orchestration, results, approvals and verified answers. Run asynchronously without blocking audio.

An `INTERRUPT` from human speech that forces playback to stop is **not** the same as a system notification choosing to interrupt the human. Distinguish `STOP_PLAYBACK` / `YIELD_TO_USER` from `INTERRUPT_FOR_EVENT`.

## 3. Product-first sequence: the real critical path

### P0 — Parity and safety before feature growth

Owner: [#32](https://github.com/kvnloo/z0live/issues/32); existing implementations: [Lawnchair PR #14](https://github.com/kvnloo/lawnchair/pull/14), broker `desktop/z0live_desktop`, inbox [#11/#23](https://github.com/kvnloo/z0live/issues/11), HTTP auth [#19/#26](https://github.com/kvnloo/z0live/issues/19). Control: [OMP `/live`](https://github.com/can1357/oh-my-pi/tree/main/packages/coding-agent/src/live).

- Prove no unsolicited or historical-replayed speech, stuck floor, repeated action, stale announcement, false completion or unacknowledged backend worker on voice interruption.
- Measure **end-of-human-speech → first *audible* assistant response**, not just first text. Measure user-interruption onset → actual speaker silence, including buffered audio, echo and headphones/speaker/Bluetooth.
- Preserve distinction between `response.cancel` (voice) and verified terminal native child cancellation (work); cancellation requested/queued ≠ confirmed.
- Audit session pinning, mute/focus/end, TTS/inbox delivery and reconnect recovery across the actual desktop route first, then owner-authorized physical phone smoke.
- A/B control differences matter: OMP currently uses `gpt-live-1-codex` and a native desktop WebRTC path; desktop z0live's `gpt-realtime` browser/phone path uses a different model and transport. Report them; do not ascribe unmeasured differences to app code.
- **Release gate:** no real audio P0 failure and no unexplained gap against preregistered OMP reference tolerance. A headless text-mode green check cannot discharge it.

### P1 — Compose, do not rewrite, the actor and broker

Owner: RFC #1 and [#20](https://github.com/kvnloo/z0live/issues/20); donors [PR #6](https://github.com/kvnloo/z0live/pull/6), [PR #7](https://github.com/kvnloo/z0live/pull/7), [protocol ledger PR #28](https://github.com/kvnloo/z0live/pull/28).

- On default branch: `desktop/z0live_desktop` has HTTP broker, pairing, sessions, history, SQLite events, inbox and Hermes native worker. In unmerged #6: `src/z0live`, deterministic actor/floor/barge/attention, gateway, provider adapter, 20 core fixtures, stress/speculation and process supervision. These are **complementary protocols**, not competing replacements.
- #7's loopback HTTP origin test had an actual **403** failure in the documented Python CI; fix the intended loopback acceptance without allowing remote origins, and re-run exact-head checks. Keep #7's PCM/provider/OMP/Tern features as donors even though native Lawnchair now leads phone entry.
- Confirm one mic → actor → chosen native harness → verified result → attention-gated speech path; preserve original commits/people, IDs, schemas, versioning, test workflows and explicit author approval/ownership.
- No duplicate task store, provider router or permission model inside desktop/portable bridge.

### P2 — Frozen quality, safety and perception evaluation

Owner: [z0evals voice study PR #86](https://github.com/kvnloo/z0evals/pull/86), [fixture issue #16](https://github.com/kvnloo/z0live/issues/16), [adapter issue #17](https://github.com/kvnloo/z0live/issues/17), existing core [PR #6](https://github.com/kvnloo/z0live/pull/6).

- Comparable 20–30 minute sessions: overlapping talk, quick interruption, correction, two async workers, approval, stale completion, background conversation, sleep/reconnect, duplicate/retry, and headset/echo cases.
- Record p50/p95/p99 frame deadline miss, first audible response, interruption→silence, false/missed interruption, jitter, underruns, echo, awkward gaps, backchannel timing, task verification, duplicate mutation, stale result, unnecessary spoken seconds and preference/naturalness. Keep resource/cost/latency/naturalness separate.
- Attach actor/host/model/version/voice/codec/route/network/fixture hash and exact-head identity. Replay-only is **PARTIAL**, never certified PASS for native audio. Do not tune on protected eval splits.
- Add: `state_packet_bytes_per_minute`, `state_packet_rebuild_latency`, `speculative_prewarm_hit_rate`, `verified_progress_per_human_attention_second`.
- Enforce that conversation state sent into actor depends on **active/relevant state**, not raw session duration. Transcript is audit/recovery, not permanent neural working memory.

### P3 — Better asynchronous agency without blocking conversation

Owner: [#27](https://github.com/kvnloo/z0live/pull/27); [VoicePlan PR #174](https://github.com/kvnloo/z0intelligence/pull/174); [OMP plugin issue #104](https://github.com/kvnloo/oh-my-pi/issues/104).

- Preserve one pinned conversational persona while routing deeper cognition through OMP/Hermes and selected workers. Reuse real native IDs, permissions, and verified terminal receipts.
- Emit minimal `StatePacketDelta`, not all raw worker messages. StatePacket carries revisions, active tasks, corrections, blockers, evidence pointers and omission/sufficiency receipts. Insufficient evidence → observe more, **not** confident success.
- Schedule attention as `SILENT` (routine progress), `WHEN_IDLE` (useful verified answer) or `INTERRUPT` (genuine urgent blocker/approval). Prefer doing correct work silently over narrating everything.
- Research [client delegation](https://developers.openai.com/api/docs/guides/live-delegation) as the production-grade donor: continuous speech is separate from application-owned task state, permissions and backend tool work; stopping model speech does **not** stop the underlying worker.

**Existing measured limits to beat:** [PR #27 receipts](https://github.com/kvnloo/z0live/blob/feat/voice-agent-stack/docs/STACK-VERIFICATION.md) report first *text* 865–1,416 ms (4 turns; not acoustic), Haiku 1.37–2.02 s, memory answer ~15 s, complex PR answer ~66 s; 54 deterministic tests and actual provider/native work. Do **not** call these p95, first speech, or a deployed Android quality SLA. Treat unknown dollar billing as unknown.

### P4 — Controlled experiments only after parity

- Donor lineup, **not a premature winner selection**: [Kyutai Moshi/Mimi](https://github.com/kyutai-labs/moshi) (80 ms audio frames, full duplex), [HITsz-TMG Lychee-FD](https://github.com/HITsz-TMG/Lychee-FD) (semantic/acoustic/control paths, early-exit dialogue control; project-reported gains), [PersonaPlex](https://github.com/NVIDIA/personaplex) (persona conditioning donor), [OpenBMB MiniCPM-o](https://github.com/OpenBMB/MiniCPM-V) (low-resource omni possibility). All reported resource/performance figures need independent reproduction on 3080 Ti 12GB under 10–30 min **p99** audio load with AEC and other GPU jobs accounted for.
- Native sequence: `DATA0` overlap/time-aligned voice events → `CONTROL0` deterministic policies → `CONTROL1` residual ambiguity → `STATE0` state-delta policy → `DUPLEX0` frozen donor → `ADAPTER0` add external-state sidecar → `PERSONA0` continuity → `SEMANTIC0` adaptation → `ACOUSTIC0` expensive retraining last.
- Cross-surface optional work in [Dash #14](https://github.com/kvnloo/z0live/issues/14), [web #15](https://github.com/kvnloo/z0live/issues/15), [Ripple #29](https://github.com/kvnloo/z0live/issues/29), [gesture #30](https://github.com/kvnloo/z0live/pull/30) and [Turbo #31](https://github.com/kvnloo/z0live/pull/31) stays gated by parity and its existing specific dependency/consent/evidence requirements.

## 4. Pre-registered high-information experiments

| Order | Experimental comparison | Must measure / abort condition | Existing owner |
|---|---|---|---|
| **A+** | Braid replay vs a second z0live task ledger | Spawn→correction→new generation→stale completion→parallel worker→cancel; reject duplicate side effects and semantic divergence | RFC #1 / #17 |
| **A+** | Compile `voice_decision.v1` from actual timelines vs session-level repeated prompts | Coverage of simultaneous user+assistant audio, partial turns, later corrections/outcomes; **halt neural training** if missing | #2 / #16 / z0evals |
| **A+** | Minimal StatePacket vs raw full transcript/event firehose | Accuracy, omitted evidence, state size, reconstruction latency, stale-result errors; retain fallback when packet insufficient | #1 / #17 / #27 |
| **A** | Deterministic floor/attention vs provider control vs learned residual | Interruption→silence, false stop, backchannel/gap timing, sealed non-inferiority | #6 / #16 / z0evals |
| **A** | `SILENT/WHEN_IDLE/INTERRUPT` scheduling vs model narrating everything | Unnecessary spoken seconds, urgent blocker delay, repeated announcements, attention-adjusted verified progress | #6 / #11 / #27 |
| **A** | Continuous client delegation→Braid→OMP/Hermes vs simple direct delegation | Backend latency, correct task IDs, steering/cancel, keep-talking, current verification, no duplicate mutations | #3 / #17 / #27 |
| **B+** | PartialIntent reversible prewarm vs no prewarm | Cold-start saving vs wasted preps; **zero side effects before authority** | Ripple #29 / future post-P0 |
| **B+** | Native 3080 Ti donor bakeoff vs production cloud actor | VRAM, 10–30 min p99 deadlines, TTFA, AEC, interruption, naturalness and GPU opportunity cost | #18 / z0int #123 |
| **B** | StatePacketDelta sidecar with frozen speech donor | Grounded silent/speak/interrupt, stale-result suppression, compression omission; no foundation training until measured | #17 / #27 |
| **B** | Heterogeneous parallel cognition vs single best provider | Independently verified correctness/time/cost/attention burden, not number of spawned agents | #27 / z0int #45 |

**Negative-result constraints** from research: do not put a universal tiny learned router in charge of hard timing; do not treat JEV confidence or Julia/Laya model outputs as authority; do not make speech completion equal verified work; do not run generic STT→LLM→TTS as the SOTA full-duplex target; do not build a second task ledger; do not share nearly all 12GB VRAM before proving frame-time headroom; never speculative-spawn a mutating worker from an unfinished utterance. Capability-specific learned components must beat simple frozen baselines on sealed/OOD tests.

## 5. Every issue and PR accounted for

The table is generated from the companion JSON manifest. **Status is as of the audit**, not a live GitHub status field. P0 is release-critical; P1 composition; P2 certification; P3 agency; P4 deferred/optional.

| GitHub | Kind | Priority | Exact role | Audit status | Research |
|---|---|---|---|---|---|
| [#1](https://github.com/kvnloo/z0live/issues/1) | Issue | P0 | RFC / canonical ownership | open | R01, R02, R03, R10, R11, R12 |
| [#2](https://github.com/kvnloo/z0live/issues/2) | Issue | P2 | historical fixtures / replay requirements | closed-not-planned | R03, R04, R06, R08, R12 |
| [#3](https://github.com/kvnloo/z0live/issues/3) | Issue | P1 | historical harness adapter contract | closed-not-planned | R01, R04, R10, R11 |
| [#4](https://github.com/kvnloo/z0live/issues/4) | Issue | P1 | historical ConversationActor and VoicePlan | closed-not-planned | R03, R07, R08, R10, R15 |
| [#5](https://github.com/kvnloo/z0live/issues/5) | Issue | P4 | historical heavy actor lifecycle | closed-not-planned | R03, R07, R13, R15 |
| [#6](https://github.com/kvnloo/z0live/pull/6) | PR | P1 | portable actor / fixtures / harness donor | open-unmerged | R03, R04, R06, R08, R10, R11 |
| [#7](https://github.com/kvnloo/z0live/pull/7) | PR | P1 | Android Tern PCM/local/OMP donor | open-unmerged | R03, R07, R12, R13, R14 |
| [#8](https://github.com/kvnloo/z0live/issues/8) | Issue | P0 | existing mobile allocator / claim board | open | R11, R12, R14 |
| [#9](https://github.com/kvnloo/z0live/issues/9) | Issue | P1 | broker builtin and client tool conformance | open-in-pr-21 | R01, R10, R11 |
| [#10](https://github.com/kvnloo/z0live/issues/10) | Issue | P1 | event replay and idempotency | open-in-pr-22 | R01, R06, R11 |
| [#11](https://github.com/kvnloo/z0live/issues/11) | Issue | P0 | SSE inbox dedupe / no replayed speech | open-in-pr-23 | R04, R06, R11, R12 |
| [#12](https://github.com/kvnloo/z0live/issues/12) | Issue | P1 | typed broker SDK | open-in-pr-24 | R01, R11, R14 |
| [#13](https://github.com/kvnloo/z0live/issues/13) | Issue | P1 | audited broker client docs/examples | open-in-pr-25 | R10, R11, R12 |
| [#14](https://github.com/kvnloo/z0live/issues/14) | Issue | P4 | Dash Expo reference adapter | blocked-sdk-integration | R03, R12, R13, R14 |
| [#15](https://github.com/kvnloo/z0live/issues/15) | Issue | P4 | web/Garden Room reference adapter | blocked-sdk-integration | R03, R12, R13, R14 |
| [#16](https://github.com/kvnloo/z0live/issues/16) | Issue | P2 | fixture integrity and provenance | blocked-portable-composition | R06, R08, R11, R12 |
| [#17](https://github.com/kvnloo/z0live/issues/17) | Issue | P1 | harness conformance / stale result | blocked-portable-composition | R01, R04, R10, R11 |
| [#18](https://github.com/kvnloo/z0live/issues/18) | Issue | P4 | actor process crash / VRAM lifecycle | blocked-portable-composition | R03, R07, R13, R15 |
| [#19](https://github.com/kvnloo/z0live/issues/19) | Issue | P0 | desktop HTTP auth and origin boundaries | open-in-pr-26 | R11, R14 |
| [#20](https://github.com/kvnloo/z0live/issues/20) | Issue | P1 | desktop vs portable protocol/evidence ledger | open-in-pr-28 | R01, R03, R11, R12 |
| [#21](https://github.com/kvnloo/z0live/pull/21) | PR | P1 | tool conformance draft for issue 9 | open-unmerged | R11 |
| [#22](https://github.com/kvnloo/z0live/pull/22) | PR | P1 | event replay draft for issue 10 | open-unmerged | R01, R06 |
| [#23](https://github.com/kvnloo/z0live/pull/23) | PR | P0 | SSE replay draft for issue 11 | open-unmerged | R04, R12 |
| [#24](https://github.com/kvnloo/z0live/pull/24) | PR | P1 | TypeScript SDK draft for issue 12 | open-unmerged | R11, R14 |
| [#25](https://github.com/kvnloo/z0live/pull/25) | PR | P1 | client examples draft for issue 13 | open-unmerged | R11, R12 |
| [#26](https://github.com/kvnloo/z0live/pull/26) | PR | P0 | HTTP security draft for issue 19 | open-unmerged | R11, R14 |
| [#27](https://github.com/kvnloo/z0live/pull/27) | PR | P3 | pinned L0 Haiku Opus Sol voice agent stack | open-unmerged | R01, R02, R04, R09, R10, R11, R12 |
| [#28](https://github.com/kvnloo/z0live/pull/28) | PR | P1 | protocol gap ledger draft for issue 20 | open-unmerged | R01, R03, R06, R11 |
| [#29](https://github.com/kvnloo/z0live/issues/29) | Issue | P4 | Ripple image annotation proposal | deferred-unverified-reference | R05, R11, R14 |
| [#30](https://github.com/kvnloo/z0live/pull/30) | PR | P4 | Oura gesture / context route / ADB opt-in | open-unmerged | R05, R11, R14 |
| [#31](https://github.com/kvnloo/z0live/pull/31) | PR | P4 | Turbo triggers and stop receipts | open-unmerged | R04, R09, R10, R11 |
| [#32](https://github.com/kvnloo/z0live/issues/32) | Issue | P0 | OMP live parity product release gate | open | R03, R04, R06, R10, R12, R13, R14, R15 |

## 6. Repo location map and immediate handoff

| Current/default location | Job | Current boundary |
|---|---|---|
| `desktop/z0live_desktop/core.py`, `server.py`, `worker.py`, `delivery.py`, `work_tools.py` | Desktop HTTP service, durable event/inbox/native work | Backend is not a native mic/speaker runtime |
| `desktop/hermes_host.py`, `bin/z0live`, `deploy/*.service` | Separate authorized Hermes service/broker CLI | No automatic new host deployment |
| `desktop/tests/`, `.github/workflows/desktop.yml` | Deterministic broker and headless opt-in integration tests | Headless text WebRTC ≠ physical speech |
| `docs/API.md`, `OPERATIONS.md`, `VERIFICATION.md`, `VOICE-TOOLS.md` | Real deployed protocol/scope/observed receipts | Documentation claims limited to observed runs |
| Open [portable PR #6](https://github.com/kvnloo/z0live/pull/6): `src/z0live`, `fixtures/`, `integrations/omp` | Provider-neutral actor/process/floor/attention/replay | **Not in default/integration baseline** |
| Open [portable PR #7](https://github.com/kvnloo/z0live/pull/7) | PCM/Tern/provider chooser/OMP donor | Unmerged; specific origin CI failure |
| Open [client SDK PR #24](https://github.com/kvnloo/z0live/pull/24): `clients/typescript/` | External HTTP SDK | Not merged/published, not Android-audio proof |
| Open [protocol PR #28](https://github.com/kvnloo/z0live/pull/28) | Existing authoritative source/evidence gap ledger | Do not edit or supersede its three source files |
| This draft: `docs/RESEARCH_TRACEABILITY.md`, sibling JSON and one offline test | Research/index/control-quality crosswalk | New files only; require Hermes exact-head verification |

**Next verified action sequence:** (1) tag P0 failures with actual audio and backend trace identities on the desktop reference; (2) reconcile P1 donors into integration without flattening protocols; (3) make the z0evals physical-audio gate reproducible; (4) use P3 cognition experiments only after conversation is stable. Leave the generated queue in #8 and the historical closed #2–#5 intact; annotate issue **bodies** with domain-specific cross-references instead of creating new parallel tickets. No task or PR in this map alone authorizes production, physical testing, default/upstream merge, or private data capture.

**Source attribution:** the prior research synthesizes Braid, AODL, z0intelligence, State Compiler, OMP, Hermes, z0evals, Evolution Lab, Tokenomics, Ripple and z0archy. Speech donor ideas and published measurements remain credited to **Kyutai (Moshi/Mimi)**, **HITsz-TMG (Lychee-FD)**, **NVIDIA (PersonaPlex)**, and **OpenBMB (MiniCPM-o)**; explicit user-derived Zer0 design hypotheses are not presented as those projects' claims.

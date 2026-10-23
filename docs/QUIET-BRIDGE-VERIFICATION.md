# Quiet voice / clean-profile bridge verification

## Host route

The deployed route is phone/WebRTC → z0live broker (`8791`) → its existing durable
`Worker` bridge → native Hermes API-only host (`8644`) in profile **clean**. The
information-only Haiku liaison runs in that same host on `8645`; persistent meta
session `z0live` uses Opus, and task sessions use the pinned Sol binding.

`/home/kvn/.hermes/profiles/clean` and the service's configured
`/workspace/hermes-home/profiles/clean` were verified to be the same directory.
This uses the profile selected by `hermes -p clean`, not a second writer attached
to the owner's interactive CLI session. No gateway, scheduler, provider fallback,
new bridge protocol, or copied profile credentials were added.

No standalone `z0bridge` component was found in this checkout's source/docs,
recent exact-term ctx search, or bounded workspace path discovery. The existing
native adapter already performs that bridge role; qualification reuses it rather
than inventing a parallel transport. A differently named external component is
not certified by these receipts.

## Deterministic regression gates

- `python3 -m unittest discover -s desktop/tests -v`: **67 tests passed**.
- `ruff check desktop bin/z0live`: passed.
- Reproduced RED cases before fixes: repeated lifecycle speech, empty status
  speech, duplicate partial findings, pending receipt speech, retained progress
  before final answer, old status rows, false liaison authority clarification,
  and condensation missing the original question.
- The actual Android client adds `speech:"silent"` continuation gating, retaining
  synchronous/parallel continuations, and frames `voice:…:result`/partial sources.
  Its JVM protocol/audio scripts passed separately from the handset.

## Real provider turn (no microphone or handset)

`desktop/tests/live_quiet_turn.mjs` ran in headless Chromium against an isolated
loopback broker store, using real OpenAI WebRTC and the existing native host.
It preserves active/creating/pending response gates and uses the same silent
acceptance policy as Android.

Observed text-channel transcript:

- User: a short greeting → direct conversational reply, **no work tool**.
- User: ask Hermes to run read-only `pwd` in the broker checkout.
- Voice: **“Understood. Checking now.”**
- One `ask_z0` dispatch: `status:queued`, `say:""`, `speech:"silent"`.
- Silent waiting; lifecycle transitions were trace-only.
- Native `terminal` started/completed; native Opus run completed; Haiku condensed
  the answer with the original question and native terminal status supplied.
- Same Realtime conversation: **“The broker checkout working directory is at
  /mnt/zer0models/workspace/zer0/products/z0live.”**
- Exactly **one** non-empty result inbox item; no status polling or additional
  waiting turns, including the 6.5-second post-result observation.

One successful sample: greeting turn 3,152 ms, work dispatch 1,259 ms. These are
text-channel measurements, not an acoustic latency percentile or an SLA.

Two further native Sol task-role runs executed read-only `pwd` and
`git branch --show-current`. Both exact run IDs were independently read back as
`completed` with actual `openai-codex/gpt-6.1-sol` runtime identity and supplied
usage. They did not speak into the ended test session.

The first live attempts exposed real failures: an unjustified execution-authority
clarification, then a correct native answer damaged by context-free condensation.
Their receipts were preserved; the successful transcript followed both fixes.

## Deployment and limits

After deterministic and real-provider gates, the production broker was restarted
only after readback showed **zero active owner sessions and zero queued/running
owner jobs**. The service was active and authenticated health/inbox readback
succeeded. The disposable health pairing was revoked and revocation read back.
The native Hermes host was not restarted.

Private receipts are under
`/mnt/zer0models/project-artifacts/z0live/2026-10-20-accessibility-bridge/`:
`broker-tests-final.log`, `real-quiet-transcript.json`,
`real-quiet-turn-context.log`, `real-native-workers.json`,
`native-readback.json`, and individual RED/GREEN logs. Provider credentials and
pairing values are not printed. Isolated stores and their native history remain
private and are not repository artifacts.

**Phone acceptance is blocked:** the S25 tailnet peer was online, but the saved
ADB connect endpoint refused connections and the exact-device reconnect fallback
was not authorized/online. No APK was installed, no Android permission was
enabled, and no phone UI/audio/screenshots were claimed. The owner must expose
its current authorized wireless-debugging connection before a handset test.
Accessibility still requires manual Android Settings enablement and per-session
consent. Host/provider proof does not certify physical microphone, speaker,
Samsung lifecycle behavior, or in-app tap/text effects.

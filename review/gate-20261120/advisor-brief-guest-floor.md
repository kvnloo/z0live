Goal: an independent, adversarial code review of one change before it is judged fit to deploy on a live voice-call
service. You are one reviewer of several; you have not seen the others' findings, on purpose.

The code is in the directory you were given with --add-dir (a plain export, no git). Read only inside that directory.

The change under review: `REVIEW-DIFF-296d018..a70a58a.diff` ("Keep GPT-Live guest exchanges on a call-local floor").
It adds `voice-adapter/guest_floor.py` (a floor state machine for two realtime voice legs, primary and guest, that
both hear the same caller microphone) and rewires `voice-adapter/mobile_live.py` (`GPTGuest`, `AudioBridge`,
`Call.message`, `Call.speak_for`, `Call.start_guest`, `Call.host_delegate`). Docs: `docs/STUDIO.md`, section
"Two voices on one call". Tests: `voice-adapter/test_gpt_guest.py`.

Everything runs on one asyncio loop. Provider events arrive per leg over WebRTC data channels; audio frames arrive
on separate tracks, so data-channel events and audio are not ordered with respect to each other.

Look for, in priority order:
1. Correctness bugs a caller would hear: wrong leg speaking, an answer spoken twice or never, the floor stuck on one
   leg, audio clipped or leaked, transcript text leaking to the phone for a muted reply.
2. Concurrency and asyncio: cancellation paths, timers (`call_later`) that outlive their state, counters that can
   drift (for example ones decremented only on success), state that is created but never cleared.
3. Regressions versus the parent: behaviour the old code enforced in code that the new code no longer does.
4. Privacy: what text crosses from one leg or one Hermes profile to the other.
5. Dead code, one-caller wrappers, naming, and whether the tests assert observable behaviour.
6. Whether the STUDIO.md table describes what the code does.

Rules for each finding:
- Cite file:line in the exported tree, and quote the line.
- Give the concrete event sequence that triggers it (the order of provider events, timer firings, or frames).
- Say whether you verified it by tracing the code end to end, or it is a suspicion. Do not pad with style nits.
- Rank by caller impact. Mark each must-fix-before-deploy or nice-to-have.

Consult the advisor twice: once after your first pass to challenge your top three findings, and once at the end to
look for anything you missed in the part of the change you read least closely.

End with a grade for the change from F to S+ and one sentence on why.
Reply in plain text, under 900 words.

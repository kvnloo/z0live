# z0live review gate, 2026-11-20

Goal: decide, for each of five z0live branches, what must change before the next deploy, graded against the bar in
`docs/REVIEW-S-TIER.md`. This is a read-only review. No branch was edited, no service was restarted, and nothing was
posted. Trace data was read for counts and timings only. No transcript text appears here.

Reviewer: burn-ultra agent. The scratch clone and this branch, `review/gate-20261120`, are based on `review/s-tier`.

## How to rerun the evidence

- `review/gate-20261120/run.sh <clone> <python>` adds detached worktrees for each reviewed head under `$TMPDIR` and
  runs the repro tests there. A FAIL is a finding still present at that head. Every test named `control` must pass
  for its finding to count. Use any clone except the production checkout. Use the adapter venv for the python.
- `review/gate-20261120/scans/*.py <trace dir>` print counts and timings from local call traces. They never print
  text. Run them with `python3 -I`.

Run on this host, as measured on 2026-11-20:

| Head | Repro file | Result |
|---|---|---|
| `a70a58a` guest-floor | `repro_guest_floor.py` | 10 FAIL, 3 controls ok |
| `f74e848` lane-status, deployed | `repro_guest_floor.py` | 10 FAIL, 3 controls ok |
| `2a3b1ff` tamil | `repro_tamil.py` | 2 of 4 subtests FAIL |
| `f74e848` lane-status, deployed | `repro_tamil.py` | 2 of 4 subtests FAIL |
| `e692bbf` fast-delegation | `repro_fast_delegation.py` | 1 FAIL, 1 control ok. Same result in 3 of 3 runs |

Each branch's own suite passes at its head. Measured with `Z0LIVE_DESKTOP` set: base `296d018` 79 tests, guest-floor
84, tamil 81, gemma-lane 83, fast-delegation 82, trace-v2 73, lane-status `f74e848` 86. All are OK with 1 skipped.

## Verdict

| # | Branch | Head | State | Grade | Must-fix before deploy |
|---|---|---|---|---|---|
| 1 | fix/guest-floor | `a70a58a` | Merged into feat/lane-status. Live: the 12:10 call ran it | **D** | 6 |
| 2 | feat/tamil | `2a3b1ff` | Merged into feat/lane-status | **A-** | 0 |
| 3 | feat/gemma-lane | `62a88cf` | Open. Merges cleanly onto `f74e848` | **A-** | 0, plus 1 owner decision |
| 4 | fix/fast-delegation | `e692bbf` | Open. Conflicts with `f74e848` | **B** | 2 |
| 5 | feat/trace-v2 | `a7bc885` | Merged, deployed | **B+** | 0 |

The 12:10:33 call ran guest-floor's exact `mobile_live.py`. Its trace `rev` is `01e04805cab6`. That equals the first
12 hex characters of the SHA-1 of `mobile_live.py` at both `a70a58a` and `f74e848`. Measured.

Guest-floor is the only branch whose bugs a caller hears today, and two of its six must-fix items already show up
in a production trace. Fix it first. Fast-delegation fixes three items from the S-tier review well, but it cannot
merge as-is, and it drops an answer at session renewal.

---

## 1. fix/guest-floor, `a70a58a` (base `296d018`), grade D

What it gets right:

- `guest_floor.Floor` is a real domain model. It holds the owner, the reason, and two answer queues in one
  dataclass, and it traces every transition as `floor.changed`. That replaces the old `addressed`/`epoch` flags.
- It fixes the S-tier guest leak (REVIEW-S-TIER 3.1). `GPTGuest.failed` closes the peer, and the audio pump exits
  on `closed`. `message` now bounds the frame size and survives bad JSON.
- The tests are hermetic. `setUpModule` in `test_mobile_live.py` stops the suite from reading the owner's
  `~/.config/z0live/voices.json`.

### Must-fix

**G1. A delivered answer is sent again 15 s later, even while the voice is still speaking it.** This is measured in
production and covered by a repro.

- Code. `flush_answers` re-enqueues the in-flight delivery once `now - sent >= 15`, whatever the reply is doing
  (`mobile_live.py:800-807`). It also arms a 15 s timer on every send (`:822`). The delivery clears only when an
  allowed reply finishes with spoken text (`:762`).
- Repro. `RedeliveryTest.test_answer_spoken_for_longer_than_15s_is_not_sent_twice` gets 2 speakable sends where 1 is
  expected. `test_unspoken_answer_is_not_resent_forever` gets 8 sends in 2 minutes. The control, a reply that ends
  before 15 s, sends once.
- Production, the 12:10 call, measured. The Hermes answer for one delegation was sent at 178.29 s. The primary
  reply began at 179.02 s and kept streaming words: 41 output transcript events by 193.29 s. At 193.29 s, exactly
  15.00 s after the send, the answer was queued again with no new delegation result behind it. The caller barged in
  at 207.89 s. The same answer was sent again at 234.90 s. That reply began at 235.88 s, and the caller barged in
  again at 239.15 s, 3.3 s into it. That the caller was cutting off a repeat is inferred.
- Fix. Requeue only when no allowed reply has started on that leg since the send. Give up after one retry and trace
  `floor.answer_dropped`.

**G2. Kevin's Hermes runs a request addressed to Tek.** This is a regression, covered by a repro.

- Code. `296d018` skipped `host_delegate` while the guest held the turn (`296d018:mobile_live.py:944`,
  `if not guest_turn`). `a70a58a` spawns it always (`mobile_live.py:1138-1140`). Only a prompt line in
  `GuestConfig.primary_note` ("say nothing and do not delegate") still stands in the way.
- Repro. In `PrimaryDelegationTest`, the caller says "Tek, delete the old release branch". The floor goes to the
  guest, the primary emits `delegation.created`, and `host_delegate` runs. The answer is then queued and spoken
  later, by design (`test_late_guest_answer_and_primary_answer_wait_for_their_floors`). The tool calls happen
  immediately, in the wrong profile. Voice turns run `terminal`, `write_file` and `patch` (REVIEW-S-TIER 2.5).
- Fix. Restore the code gate. Drop or hold primary delegations created while `floor.owner == "guest"`, and trace
  them. The guest side already drops its own off-floor delegations (`:850-854`), so both legs follow one rule.

**G3. Any "back to X" or "over to X" redirects the floor to the primary, including "Tek, back to you".** Covered by a
repro.

- Code. `Addressing.handoff` matches any word after "over to", "back to" or "let's hear from"
  (`guest_floor.py:28`). The latest match wins (`:36`), and every name that is not a guest name becomes
  `Address.OTHER` (`:37`). Only the final transcript applies it (`:33-34`).
- Repro. `AddressingTest`:
  - "Tek, back to you" returns OTHER.
  - "Tek, go back to the first point" returns OTHER.
  - "can we go back to the budget" returns OTHER where it should return None.
- `FloorIntegrationTest`. Their own test phrase "Tek, back to you" keeps the guest on the floor while non-final.
  The final transcript then moves the floor to the primary with reason `redirected`.
- Fix. Treat a handoff target as OTHER only when it is a known name: `hermes`, `primary`, the primary's name, or a
  configured third party. Otherwise return None. Never let a handoff override a vocative to the guest earlier in
  the same turn.

**G4. Primary audio still playing at `turn.done` is muted when a GPT guest is on the call.** Covered by a repro. The
production signature is measured.

- Code. `observe` pops the reply at the provider's `turn.done` (`mobile_live.py:751`). `output()` then silences
  every primary frame, because no reply is open (`:906-918`). WebRTC audio keeps arriving after `turn.done`.
- Repro. In `ReplyTailTest`, a frame passes while the reply is open, which is the fixture check. The next frame after
  `turn.done` comes out silent.
- Traces, measured with `scans/scan_tail.py`. I took each assistant `turn.done` that arrived while phone-bound
  audio was on and measured the time to the next output `audio.off`.
  - Base revs, no guest, n=53: p50 0.44 to 0.54 s, p90 1.67 to 2.76 s, max 5.6 s.
  - `a70a58a` with a GPT guest, n=5, one call: p50 0.28 s, max 0.33 s.

  `audio.off` fires after 0.30 s of quiet (`LEVEL_OFF_SECONDS`). Subtract that window to get the sound itself. In
  the base, sound ran on p50 0.14 to 0.24 s past `turn.done`, up to 5.3 s, and 9 of 53 replies had more than 0.7 s
  of it. At `a70a58a`, 0.28 to 0.33 s means the sound stopped at `turn.done`. With n=5, short replies could also
  explain that. The repro shows the mechanism directly.
- Fix. Keep the reply open until the output level goes quiet, the same signal `Level` already computes, not until
  `turn.done`.

**G5. Answers on the floor leg now queue behind the current reply, and lane answers go stale in the queue.**
Measured in one call.

- Code. `speak_for` always enqueues when a GPT guest exists (`mobile_live.py:1247-1253`). `flush_answers` sends only
  when that leg has no reply open (`:810-812`). `tier_lanes` decides whether a lane answer is stale when it is
  queued (`:1374`), not when it is sent. In `296d018`, `speak_for` sent at once, and the model merged answers.
- 12:10 call, measured with `scans/scan_resend.py`. Three lane answers for the first delegation were queued at
  82.4 to 83.9 s. Hermes finished at 98.11 s. Two of the lane updates were still sent after that, at 101.63 s and
  104.22 s. The Hermes answer went out at 120.21 s, 22.1 s after it was ready. The other two Hermes answers in the
  call waited 12.2 s and 25.2 s. n=3. All 3 waits are on top of a delegation that already takes p50 39.7 s
  (REVIEW-S-TIER 4).
- Fix. Queue only across floors. When the answer's leg owns the floor, send it as `296d018` did. Re-check
  `hermes_done` and `latest_did` at delivery, and drop stale lane answers.

**G6. A second question to Tek, asked 20 s or more after the first, is answered by nobody.** The Fable advisor found
this. I verified it with a repro and a control.

- Code. When Tek already owns the floor, `Floor.change` does nothing (`guest_floor.py:66`), so `since` keeps the
  time of the first grab. `user_turn` still clears `replied_at` (`:76`). `expire` then measures from `since` (`:90`).
  The next `flush_answers` moves the floor to the primary with reason `guest_idle`. Any guest event while Tek owns
  the floor triggers that flush (`mobile_live.py:855-856`). Tek's reply then opens with `allowed=False` and is
  muted. `primary_note` tells the primary to stay silent when Tek is named.
- Repro. In `ReAddressTest`, Tek is addressed at 100 s and finishes at 118 s. At 122 s the caller asks "Tek, and
  what about tomorrow?". The floor goes to the primary, and Tek's answer is muted. The control asks the same thing
  at 114 s, 14 s after the first grab, and Tek keeps the floor. In a conversation with Tek, the second question
  usually comes more than 20 s after the first. Inferred.
- Fix. A guest address while Tek owns the floor restarts the idle clock. Set `since = now` in the `Address.GUEST`
  branch. Add the advisor's sequence as a test.

### Nice-to-have

- An outstanding guest delegation pins the floor on the guest (`guest_floor.py:81,89`). Unnamed turns cannot take
  it back until `ask_guest_host` returns, and that can take up to `DELEGATION_TIMEOUT_SECONDS = 600`
  (`mobile_live.py:193`).
- After a barge-in, the cut answer is queued again and replayed. That matches the docs. In the 12:10 call it
  happened once: the caller cut in at 122.19 s and the answer was sent again at 131.43 s. A barge-in usually means
  "stop". Make the replay a product decision, not a default.
- A guest that fails at setup stays as `self.guest` (`:1227-1229`). The primary then announces "tek disconnected"
  for a guest that never connected, and every primary answer runs through the floor queue for the rest of the
  call. REVIEW-S-TIER measured setup failure in 7 of 7 earlier guest calls.
- `note_budget` stops cross-leg notes after 12,000 characters per destination (`:644`) and traces nothing when it
  runs out.
- There is dead and duplicated code. `Level(off_seconds=...)` has no caller that passes it any more (`:519`).
  `silence()` and `_SILENCE` have one caller (`:504-509`). `guest_floor._WORD` and
  `call_trace._REACTION_WORD_CHAR` are two definitions of a Tamil-aware word character.
- `test_gpt_guest.py` reaches into internals: `g.audio.extend`, `g.floor.change`, `g.floor.reason`. The asserts
  on `floor.owner` are fine, because the floor is the domain model. The others will break on a refactor that keeps
  behaviour.
- `docs/STUDIO.md`, "Two voices on one call", does not mention the 15 s requeue or the floor pin while a guest job
  runs.
- A short barge-in is ignored when its letters appear anywhere in the current reply. The advisor found this, and
  `ShortBargeInTest` confirms it, with a control. `interrupt` checks `words in self.last_output` as a character
  substring, with no length floor (`mobile_live.py:773`). A caller's "No" does not cut a reply containing "know"
  or "now". Each provider still hears the caller itself, so the audible cost depends on its own turn detection.
  Inferred. Compare whole words, not characters.
- The test that proved the guest host gets tek's key, not the primary's, was deleted
  (`296d018:test_gpt_guest.py:93-109`). `a70a58a` mocks `ask_guest_host` instead (`test_gpt_guest.py:136`), so
  nothing tests credential isolation between profiles. The advisor found this, and the diff confirms it. Restore
  the test.
- After a guest failure, nothing tells the primary that Tek has gone. `primary_note` still says to stay silent when
  Tek is named. Advisor, inferred.
- `latest_address_ms` compares `start_ms` values from two provider sessions that started at different times
  (`mobile_live.py:695`). Advisor, inferred, not measured.
- Privacy, inferred, low. Primary answers reach the guest leg as developer notes. The guest model can include them
  in a request to the tek profile's Hermes, whose provider may differ from the one that heard them on the call.

---

## 2. feat/tamil, `2a3b1ff` (base `296d018`), grade A-

It is small and trace-only. Its tests drive real `turn.done` events through `Call.message` and read the JSONL back.
The docs math is right: 500 bytes divided by 3 bytes per Tamil code point gives 166. It labels the token bound as
inferred.

Must-fix: none. `user.reaction` feeds no decision path. Only trace readers use it.

Nice-to-have:

- **T1. Plain Tamil negation is labelled a correction.** Covered by a repro. English `no` counts only before a comma
  (`no(?= *,)`). `இல்ல`, `இல்லை`, `illa` and `illai` count anywhere (`call_trace.py:18`).
  `TamilPrecision` shows that "எனக்கு நேரம் இல்லை" ("I have no time") and its romanized form return
  `correction=True`. The English control "I have no time today" returns False. Apply the English rule, which
  needs a turn-initial position or a following comma.
- NFC applies to the reaction text (`:102`) but not to the `repeat` tokens (`:99`). An NFD and an NFC version of
  the same Tamil turn are not counted as repeats. Normalize once, before both.
- `test_tamil_chunks_use_utf8_bytes_not_code_points` pins the exact split `[498, 102]`
  (`test_mobile_live.py:43`). Assert at most 500 bytes per chunk and a lossless join. Any split policy that keeps
  those rules should pass.

---

## 3. feat/gemma-lane, `62a88cf` (base `296d018`), grade A-

It merges cleanly onto `f74e848` (measured with `git merge-tree`). `local_chunks` rejects any non-loopback, query
or userinfo URL before it connects (`lane_server.py:27-31`). It treats a stream with no `[DONE]` as a failure,
not a partial answer. The tests run real HTTP servers and assert the body the model server receives. The bench
refuses the production lane port. Every llama-server flag in the plist exists in build b12527 (measured with
`--help`).

Must-fix: none.

Owner decision before deploy:

- **The Gemma server starts on the next install, whether or not `tiers.json` uses it.** `install.sh:94-99`
  registers `com.zer0.z0live.gemma` with `KeepAlive` whenever the binary and the model exist. Both exist on this
  Studio (measured). The model is 4,590,807,392 bytes and loads on Metal with `--gpu-layers all`. Only the lane
  routing is opt-in. The docs say so accurately (`docs/STUDIO.md:112-115`). Decide whether an always-resident
  4.6 GB model earns its memory before any tier uses it. If not, gate registration on an explicit flag.
- `install.sh` still boots out all services, including the voice adapter. The branch adds "Do not run the installer
  during a call" to the docs. Good, and it means the deploy needs an idle window.

Nice-to-have:

- `first_s` has two definitions. The local path starts the clock at the first non-whitespace piece
  (`lane_server.py:63`). The hosted path starts at the first non-empty piece (`:75`). `bench/gemma_race.py`
  compares `first_s` across the two arms. The doc line at `docs/STUDIO.md:122` describes only the local path.
  Measure both the same way.
- `test_local_stream_measures_first_visible_text` feeds exactly three `monotonic()` values (`test_lane_server.py:63`).
  One more clock read in `ask` raises `StopIteration` even when behaviour is unchanged.
- When a race loses, the adapter drops the connection, but the lane server thread keeps reading llama-server's
  stream until `max_tokens` (`lane_server.py:41`). With `--parallel 2`, that holds a slot. Inferred, not measured.

---

## 4. fix/fast-delegation, `e692bbf` (base `296d018`), grade B

It closes three S-tier items at their root, each with a behaviour test:

- No more chat-fallback double run (REVIEW-S-TIER 2.5 High). See the comment at `mobile_live.py:1344` and
  `test_lost_legacy_reply_never_falls_back_to_oneshot`.
- No resume retry after a non-zero exit (`test_failed_resume_does_not_repeat_a_possibly_executed_request`).
- The voice never reads `str(exc)` aloud (`:1151`).

The delegation job is now tracked in `self.tasks`. The per-call Responses session and the streamed first sentence
are a direct answer to REVIEW-S-TIER 4.1. The renewal test runs over real WebRTC.

Must-fix:

**F1. It does not merge with the deployed branch.** Measured. Merging `f74e848` into `e692bbf` conflicts in 2 hunks
of `voice-adapter/mobile_live.py`, the mic and track wiring in `Call.__init__`, and in 1 hunk of `docs/STUDIO.md`.
Both branches rewrote the same audio path. Guest-floor routes the metered mic through one `MediaRelay` to the
provider and the GPT guest. Fast-delegation routes it through `SubscriptionLeg` so it can swap legs. Rebase onto
`f74e848`, and add one test that renews a call with a GPT guest present. The merge also inherits these risks,
inferred from reading both:

- `replace_subscription` does not resend `guest.config.primary_note`. The new primary leg forgets the guest exists.
- A primary reply still open in `GPTGuest.replies` at the swap gets no `turn.done` from the retired leg. The primary
  queue in `flush_answers` then waits forever.

**F2. An answer still in flight at session renewal is dropped, with no trace.** Covered by a repro, with a control.

- Code. `replace_subscription` waits for in-flight hand-offs only until `deadline - 2` (`mobile_live.py:1405-1407`).
  The lead is 45 s (`:1374`). It then clears `protocol.seen` (`:1423`). `Protocol.from_mobile` drops any answer
  whose id is not in `seen` (`:472`). So `host_delegate` sends nothing, and `delegation.done` is the only record.
- Repro. In `RenewalDropsAnswer`, the hand-off is still running when the 3 s renewal budget ends. After the swap,
  Hermes answers, and the new provider channel never receives the answer. The control, with no renewal, delivers
  it. 3 of 3 runs.
- Frequency. GPT-Live granted 7,199 to 7,200 s per session in all 10 traces that carry `expires_at` (measured with
  `scans/scan_expiry.py`). So renewal happens once per 2 h of call. A delegation running at that moment loses its
  answer whenever it outlasts about 43 s. REVIEW-S-TIER 4 measured p50 39.7 s and p90 166 s.
- Fix. Keep the ids of pending delegations across the swap, or deliver late answers as speakable text with no
  `delegation_id`. Trace `delegation.dropped_after_renewal` if neither works. Guest-floor's `flush_answers`
  already falls back to `did=None` (`a70a58a:mobile_live.py:818`). Reuse that rule.

Nice-to-have:

- `test_mobile_live.py` at `e692bbf` reads the owner's `~/.config/z0live/voices.json`. With the guest set there,
  the integration fixture opens a third provider channel. I measured 3 where my repro expected 2. Guest-floor's
  `setUpModule` fixes this. Keep it in the merge.
- `primary_host.SENTENCE` (`primary_host.py:13`) splits after any `.`, `!` or `?` followed by whitespace. "e.g. "
  ends the "first sentence" early.
- Each call stores a `z0live-primary-<id>` conversation in the poteto profile, and nothing prunes them.

---

## 5. feat/trace-v2, `a7bc885` (base `3d4363a`), grade B+

It moves `Trace` out of `mobile_live.py` into `call_trace.py`, which deletes the inline class. It records what was
actually sent to each provider. It hashes the persona instead of storing it. It writes files as 0600. Measured: the
3 trace files written after it deployed are `-rw-------`, and the earlier ones are `-rw-r--r--`. Its tests read
the real JSONL back. It also fixes the false "nothing persisted" docstring from REVIEW-S-TIER 1.4.

Must-fix: none.

Nice-to-have:

- `mkdir(mode=0o700)` sets the mode only when it creates a directory. `voice-traces/` and `2026-11-20/` are still
  `drwxr-xr-x` (measured). The `~/.local/share/z0live` root is `drwx------`, so other users cannot reach them on
  this host. Set the modes explicitly, or drop the claim.
- There is still no retention. REVIEW-S-TIER item 10 is open. Today's traces are 0.3 to 2.3 MB per call (measured).
- `context.sent` stores every context append verbatim (`call_trace.py:40-42`). That includes the turn-0 call brief,
  which is built from Hermes chat history: 2,933 to 3,511 bytes per call (measured with
  `scans/scan_context_sent.py`, 3 calls). The owner's rule in
  `REQUIREMENTS.md` sends traces to call providers but keeps chat history local, and says to treat unclear data as
  private. No code exports these rows today (checked with `git grep`). Tag brief rows with a `source`, so a future
  exporter can drop them.
- `docs/TRACE-BOUNDARY.md` line references were wrong even at `a7bc885` (measured):
  - `mobile_live.py:679,703` should be `:657,687`, where `tx` and `rx` are traced.
  - `:880,932` should be `:879,931`.

  On the deployed head, every one of those references points somewhere else. Cite symbols, not line numbers.

---

## 6. Fable advisor pass on guest-floor

I asked for one independent pass on the riskiest branch. Guest-floor qualifies because it is live and holds the
most state. The run was `advisor-lane --model sonnet --allowedTools "Read Glob Grep" --timeout 1500`. It used seat
claude2, path FREE and 2 advisor consults. It took 15 turns, 605 s and $3.68, as reported by the lane's JSON.

What it saw: a plain export of `voice-adapter/` and `docs/STUDIO.md` at `a70a58a`, plus the diff. It got no git
history, no traces, no chat data and none of my findings. Its brief is
`review/gate-20261120/advisor-brief-guest-floor.md`. Its session could not run Python, so every item it gave is from
reading the code.

| Advisor item | My check | Outcome |
|---|---|---|
| 15 s requeue speaks an answer twice | Repro plus production trace | Agrees with G1 |
| "back to" and "over to" hijack the floor | Repro | Agrees with G3 |
| Primary delegation runs while Tek holds the floor | Repro | Agrees with G2 |
| Audio passes only while a reply is open, so tails may clip | Repro plus trace signature | Agrees, as G4. The advisor left it a suspicion |
| Re-addressing Tek after 20 s mutes Tek | Repro plus control | **New, verified.** Added as G6, must-fix |
| Substring echo test swallows short barge-ins | Repro plus control | New, verified. Nice-to-have |
| Key-isolation test deleted | `git grep` on both commits | New, verified. Nice-to-have |
| Provider events may lack `turn_id`, `start_ms`, `end_ms` | `scans/scan_event_shapes.py` on the 12:10 call | **Disconfirmed.** 449 of 449 primary and 218 of 218 guest `turn.delta` events carry all three. All 13 primary and 11 guest user `turn.created` and `turn.done` events carry `id`, `start_ms` and `end_ms` |
| Cross-session `start_ms` comparison | Not measured | Kept as inferred |
| Primary keeps ignoring Tek after a guest failure | Not tested | Kept as inferred |

Items I found that the advisor did not: G5, the queue latency and stale lane answers, and all production
measurements behind G1 and G4.

Grade. The advisor gave C. I give D. The gap is evidence the advisor did not have. Two must-fix items show up in a
production call, and G5 adds 12 to 25 s to Hermes answers. With G6 verified, the second exchange with Tek fails
whenever it starts 20 s or more after the first.

---

## 7. Method and limits

- Diffs are each head against its merge base: guest-floor, tamil, gemma-lane and fast-delegation against `296d018`,
  and trace-v2 against `3d4363a`. I read every hunk in `mobile_live.py`, `guest_floor.py`, `primary_host.py`,
  `subscription_leg.py`, `lane_server.py` and `call_trace.py`. Docs and tests were read for their claims.
- Trace numbers come from 13 local trace files from 2026-11-19 and 2026-11-20. Only one call, 12:10:33, ran
  guest-floor, so the G1, G4 and G5 production numbers have n=1 call, and each repro carries the claim. Where n is
  small, I give it.
- Production repo. The only command I ran in `~/workspace/repos/z0live` that changes anything was
  `git fetch --all --prune`. It updates remote-tracking refs only, not the working tree or any branch. Every other
  read used `git show`, `git diff` and `git merge-tree`, or ran in my scratch clone.
- Not covered:
  - Live audio, so G4's audible clipping is inferred from levels.
  - Whether audio starts before the reply opens, which would clip heads too. My scan could not tell a reply's
    first sound from a pause inside a reply, so I dropped it.
  - Memory use of the Gemma server.
  - A merged build of fast-delegation onto lane-status.
  - Whether Hermes's Responses stream marks commentary-phase deltas. Fast-delegation's first-sentence path assumes
    it does not.

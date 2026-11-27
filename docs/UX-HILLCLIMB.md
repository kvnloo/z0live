# z0live TV UX hill climb: rubric, scoring and round ledger

Goal: the z0live TV surface (the call views and the dashboard views on the 85-inch Samsung) should feel
magical from the couch, not merely acceptable. This file is the rubric and protocol a hill-climb runner
follows: one change, a before/after score from rendered screenshots, keep only wins.

The TV app lives in `kvnloo/atlas-engine` (local checkout `atlas-engine-tv`) (`apps/z0live-tv`, branch `feat/z0live-tv`). This repo holds the
rubric because the TV is the call's visual channel. Requirements are in `docs/REQUIREMENTS.md` and the
TV requirement list R1-R17 (whole app, ephemeral, factory, taste/retro/product, home/lanes).

## View families

| family | views | couch question |
|---|---|---|
| Call views (shown while Kevin talks) | `ephemeral`, `ask` | What did the call just produce? Which lane is holding up my request? |
| Dashboard views | `home`, `factory`, `product`, `retro`, `timeline`, `artifact`, `lineage`, `research`, `taste` | What needs me, what is moving, where did time go? |
| Helper | `legend` | Which remote key does what here? |

Per-view primary question, supporting-fact task and useful-surprise task are frozen in the climb's
`tasks.json` (copied below under "Per-view tasks").

## Rubric (100 points, 20 checks, pass/fail)

Five dimensions, four checks each. Dimension rating = checks passed / checks available. Score =
`30*A + 25*F + 20*T + 15*M + 10*U` (each a fraction), out of 100.

| dim | weight | Kevin's requirement it encodes | checks |
|---|---:|---|---|
| A: across-room reading | 30 | words, not only dots (R2); labels survive reload (R5) | A1 title readable on the couch proxy; A2 the primary answer readable on the couch proxy; A3 three supporting labels readable on the mild proxy; A4 primary answer identical after reload |
| F: fast understanding | 25 | self-explaining (R3); answer first | F1 what the view is about is obvious at a glance; F2 the primary question is answered on screen; F3 the supporting fact can be read; F4 a next step is shown, or the screen says truthfully there is none |
| T: trust | 20 | counts that match (R15); live, never stale (R6) | T1 names, counts and ordering agree (headline = parts); T2 time window and recency explicit; T3 live / historical / missing / estimated states distinguished; T4 a visible source supports the answer |
| M: motion and instant feedback | 15 | update in place, no flash (R4); smooth like Netflix (R1, R9) | M1 entry motion is purposeful, nothing overprints or jumps; M2 unchanged text stays put between frame and reload; M3 view control/route gives the expected state with a readable focus cue (native); M4 remote acknowledgement <= 250 ms p95 over 20 presses, 0 dropped (native) |
| U: wow / useful surprise | 10 | canvas mode: agents pull relevant charts and stats onto the screen while he talks (R13) | U1 find the useful detail in 10 s without a hint; U2 it connects to the primary answer; U3 it changes a decision; U4 that decision is backed by what is on screen |

Hard gates (fail the round regardless of total): a missing main label, a materially wrong answer, stale
data shown as live, overprinted or stacked text, a refresh that steals remote focus.

### How each check is scored from a screenshot or replay

Renders come from the live TV server (`serve.py`) through the same text path the TV uses (`textimg=1`: text
baked by `textlayer.py`), so a desktop shot shows what the TV shows. The TV itself allows no screenshots.

| image | size | used for |
|---|---|---|
| `<view>.png` | 1920x1080 | F, T, U checks; collision gate |
| `<view>-couch.png` | 640x360 | strict proxy for 85" at couch distance: A1, A2 |
| `<view>-couch960.png` | 960x540 | mild proxy: A3 |
| `<view>-reload.png` | 1920x1080 after a fresh reload | A4, M2 (byte or text-position compare) |
| `<view>-t03.png` | 0.3 s into the entry animation | M1 |

- Reading checks are strict: pass only if the scorer can transcribe the exact string from the named image.
- M2 and A4 are mechanical (md5 or per-label position diff); the collision gate in `render.mjs` fails M1/T1
  on any overlapping text boxes.
- A vision scorer looks at each image and records `[pass, evidence]` per check in `scores.json`.
- M3 and M4 need the native app and the remote; desktop-only rounds report coverage 18/20 and renormalise.
  Native checks are run from Kevin's seat before a view is called done.
- Two runs per arm or a parent-vs-parent null pass give the noise floor; a keep needs a gain above it.

Keep rule: the weighted score rises, no hard gate fails, and A, F and T do not drop. Stop a view after
3 misses in a row or 8 tries. Target: every view >= 85 with A >= 0.75, and the call views >= 90.

## Per-view tasks

| view | primary question | supporting fact | useful surprise |
|---|---|---|---|
| ephemeral | What did this call just produce or put in motion? | status and fact on the new card | a finished receipt or a call item worth reopening |
| ask | Which lane is holding up this request? | its status and one result, source or unknown | a lane result that changes the next action |
| home | Which workstream is active now? | the live chat that explains it | the workstream worth opening next |
| factory | What needs my attention first across bots? | the bot, project or trace signal behind it | high-priority dropped work and why to resurface it |
| product | Which workstream is blocked and what comes next? | the blocker and its owner | a next card that can move despite the blocker |
| retro | Where did the most agent time go? | amount or share and cause | the ranked time sink with a concrete fix |
| timeline | What changed most recently and where? | time and workstream or repo | a related handoff or commit |
| artifact | What did the agents make? | the ask that produced it | a frame that resolves a question |
| lineage | Which version are we at and what led here? | a milestone on the time axis | a recurring ask linked to a milestone |
| research | Which experiment should happen next? | evidence or uncertainty behind it | a rival hypothesis it distinguishes |
| taste | What is this entity connected to? | relationship type and strongest neighbour | one evidenced lineage link worth following |
| legend | Which key performs the action I want here? | current view and key behaviour | the context action that avoids a wrong press |

## Where things live (out of the tree)

- Harness: `render.mjs` (Playwright render of every view through the TV text path + collision gate),
  `SCORING.md`, `tasks.json`, `runs/<round>/` (shots, `summary.json`, `scores.json`). Current copy:
  tek workspace `z0live-reset/K-climb/`.
- Progress log per round: tek workspace `z0live-reset/K-PROGRESS.md`.
- Code changes: `atlas-engine-tv` branch `feat/tv-climb-k` (merged into `feat/z0live-tv` when kept).
- Ideas (Astra ideation + Fable advisor review) and the pick list: section "Ideas" below.
- N215 round evidence (home view): tek workspace `z0live-reset/N215-climb/` (runs, blind judge inputs, Astra brief and
  output, Fable prompt and picks, render scripts).

## Ideas (2026-11-27)

Ideator: Codex `gpt-6.2-astra` (normal tier, read-only), given the requirements R1-R17, this rubric, the baseline
scores and how Kevin uses the call + TV. Its sandbox could not open the files, so it worked from the brief alone and
its score estimates are unverified. Reviewer: one Claude Code lane, Sonnet main with the Fable 5.2 advisor (2 advisor
consults, free harness path), which checked the 12 ideas against the real baseline evidence.

| # | Astra idea | views | Fable verdict | status |
|---|---|---|---|---|
| 7 | Every lane gets a verb; re-scoped to one "Holding up: <lane> · <state>" line from real lane state | ask | PICK (call 1): F2, A2 | open; ask already 51.25 -> 83.75 from the card-fit fix (K r2) |
| 4 | Show what the call just touched: "You asked <ask> -> <receipt> · OK reopens", conductor pairing only | ephemeral | PICK (call 2): A2, U4, U3 | partly done: headline = the call's newest change (K r2, 87.5 -> 95.0) |
| 6 | Home acknowledges the live call, plus a next step | home | PICK (dash 1): F4, U1-U3 | DONE, N215 r1 (83.75 -> 97.5) |
| 3 | an attention count you can inspect, reduced to a reconciled count | factory | PICK (dash 2): T1 | tried by K r2 (count reconcile): MISS, trace labels still overprinted by bars |
| 1 | Fixed ask strip above the work | ephemeral, ask | not picked this wave | backlog |
| 8, 10, 11, 12 | lineage sentence, product before/after, research source footer, taste example | dashboard | not picked this wave | backlog |
| 2 | "Reading aloud" badge on the spoken lane | ask | REJECT: depends on playback timing no screenshot can see | needs a replay metric first |
| 5 | Restore focus after Back | ask, artifact | REJECT for desktop rounds: native-only (M3/M4) | native round |
| 9 | Hold the timeline still on new activity | timeline | REJECT: timeline's real failures are the headline checks | replace with "Latest: <item> · <time> · <workstream>" |

Rubric gaps the advisor flagged (to fix before trusting high scores):
- Headline checks reward form, not truth: a wrong "Holding up: X" scores like a true one. Every new answer line
  needs a fact check against its source in the round notes.
- Nothing measures speed or in-place updates across a data change (R1, R4); M2 compares only static reloads.
  Next metric: view-switch time and changed-pixel area on a scripted data change.
- The call views were scored after the call ended, never in the live state. Next call: render ephemeral/ask
  during the call from the lane-local server (no deploy needed).
- Every view weighs the same; home and ephemeral matter more to Kevin than lineage or research.

## Round ledger

Scores are provisional desktop scores over 18 of 20 checks (M3/M4 need the TV). "Blind" = a fresh reviewer scored
both arms without knowing which is which, sides swapped between rounds.

| round | family | view | change | before | after | verdict | commit | shots (tek workspace `z0live-reset/`) |
|---|---|---|---|---|---|---|---|---|
| K r1 | gate | artifact, ask | textlayer anim layers; ask cards fit | collisions 10 | 0 | KEEP | a8b8a1e, a5eb856 | `K-climb/runs/c1/` |
| K r2 | call | ask | lane cards fit their height | 51.25 | 83.75 | KEEP (blind) | a5eb856 | `K-climb/runs/r2/{base,cand}/ask*.png` |
| K r2 | call | ephemeral | headline = the call's newest change | 87.5 | 95.0 | KEEP (blind) | e1e36b0 | `K-climb/runs/r2/{base,cand}/ephemeral*.png` |
| K r2 | dash | artifact | animated captions in their own layer | 71.25 | 88.75 | KEEP (blind) | a8b8a1e | `K-climb/runs/r2/{base,cand}/artifact*.png` |
| K r2 | dash | factory | "First of 47" headline + count reconcile | 87.5 | 87.5 | MISS 1/3, reverted | - | `K-climb/runs/r2/{base,cand}/factory*.png` |
| N215 r1 | dash | home | "next" line (call live -> OK opens the call view; else busiest live workstream and why; else "nothing needs a look") | 83.75 | 92.5 | HOLD: T1 failed (header strip 477 vs chart 484, two clock reads) | - | `N215-climb/runs/home-{base,cand}/` |
| N215 r1b | dash | home | + one tick read and one clock per frame | 83.75 | 97.5 | KEEP (blind, sides swapped) | 5f3801c | `N215-climb/runs/home-{base,cand}-r1b/`, judge inputs `N215-climb/runs/blind2/` |
| N215 r1c | dash | home | + 4 live rows, so the 24 h chart axis stays above the footer rule (`test_views.py` failed with 5) | tests FAIL | tests pass, counts 335 = 335 | KEEP (self-check) | 5f3801c | `N215-climb/runs/home-{base,cand}-r1c/` |

Known issue found in the baseline, not yet fixed: home's live list says "5 of 6 workstreams" while the headline says
7 (`data.live_rows(sess, 6)` caps the list at 6 before counting).

## Continuing

The climb continues on the existing scheduled runner, tek cron `z0live-research-scientist` (id `a2cf4fac57bc`,
every 15 min), as Track U in `~/.local/share/z0live/research/PROGRAM.md`: one view change per wake with this rubric,
the same render + blind-judge protocol, and a ledger row. Next targets in order: ask "Holding up" line,
ephemeral ask -> receipt pairing, factory trace-label plates (M1/T1), timeline "Latest" headline, home live-list
count, then the speed/in-place metric from the rubric gaps.

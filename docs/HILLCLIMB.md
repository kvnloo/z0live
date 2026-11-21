# z0live hill-climb

Goal: Dessa-level conversation with Hermes-level intelligence (`docs/REQUIREMENTS.md`). This file is the
program a scheduled agent follows. One change, one measurement, keep or revert.

## Metric (frozen 2026-11-19)

Harness: `voice-adapter/bench.py` on `bench/suite-v1.json` (12 cases: 6 spoken reasoning, small talk, 3
must-delegate Hermes requests, 2 chat-context cases), 2 repeats, both engines interleaved, real providers,
synthetic speech, simulated phone over WebRTC, delegation as a fixture.

- **Metric:** suite pass rate per engine (a case passes when there is no catch-up and no refusal, the voice
  delegates exactly when it must, and the reply or request matches).
- **Gate (must hold for a keep):** neither engine's pass rate drops; first-audio p50 does not rise more
  than 15 %; adapter unit tests and `desktop/tests/test_lanes*.py` stay green; zero harness errors.
- **Stop predicate:** both engines >= 0.95 pass rate AND first-audio p50 <= 0.80 s AND at least 10
  iterations run. 0.80 s is this synthetic-PCM rung; the Dessa targets (EOS -> first *audible* p50 <= 500
  ms, barge-in silence p95 <= 150 ms) need the physical stratum below and are the next suite.
- **Noise:** a keep needs a gain larger than the spread between the two repeats of the same arm.

Never edit `suite-v1.json` after its baseline. New cases go in `suite-v2.json` with a fresh baseline.

## Where things live (out of the tree)

- Receipts: `~/.local/share/z0live/bench/*.json` (+ `.log` with one row per case).
- Decision log: `~/.local/share/z0live/bench/decision.tsv`, columns
  `id	at	hypothesis	change	before	after	delta	tests	verdict	note`. Read it before each attempt.
- Work happens in a worktree: `git -C ~/workspace/repos/z0live worktree add ~/workspace/wt/z0live-hc-<id> feat/lane-status`.

## Iteration protocol (one per scheduled run)

1. Refuse to run if a call is open: any file under `~/.local/share/z0live/voice-traces/` modified in the
   last 10 minutes. Log `skipped: call open` and stop.
2. Read `decision.tsv` and the latest receipt. Pick the top untried hypothesis below (or one the log
   suggests), name its mechanism, and write the row's hypothesis before measuring.
3. Make the one change in a worktree. Run the gate tests, then `bench.py` from the worktree.
4. Keep only if the metric beats noise and the gate holds. Keep = one commit (stage only the files you
   changed) on `feat/lane-status`, push, then `deploy/macos/install.sh` only when no call is open.
   Otherwise discard the worktree.
5. Log the row either way. Hand-check every failing case row before calling a regression.
6. Never change credentials, the phone, other hosts' services or upstream repos from this loop.

## Hypothesis backlog (grounded in traces and the 11-16/11-17 failures)

| id | Mechanism | Expected move |
|---|---|---|
| H1 | Delegation rules in the persona: hand off device/data/action requests, answer reasoning and small talk itself | `delegation` checks, both engines |
| H2 | Grok `reasoning.effort` none vs high (hermes-xai defaults to none for speed) | first-audio vs reasoning pass |
| H3 | Grok server VAD `silence_duration_ms` 500 -> 300 | first-audio p50 |
| H4 | Brief framing: state standing approvals as facts the voice may act on | `x-approval`, `h-screen` |
| H5 | Planted-fact recall: put chat facts in the brief, not only in lane context | `x-fact` |
| H6 | Chat anchoring for calls from a new chat (`history_items 0`): fall back to the newest active session of the profile (11-17 real calls had `chat.resolved ""`) | needs suite-v2 case with an empty history |
| H7 | Warm Haiku instant lane on vs off: does a lane answer pre-empt a better model answer | reasoning pass, first audio |

## Devices

| Device | Role in the climb |
|---|---|
| Mac Studio | Runs the bench, both provider logins, the voice stack and this loop |
| groot | Local lane models (llama-servers 11530/15637, down 11-19) and k3s worker (NotReady since 11-17) |
| MBP | Build node once its disk (96 %) is cleaned; k3s worker |
| S25 Ultra | Physical stratum: real Hermes-app calls, physical onset/silence timing (adb, tailnet port changes on restart) |
| S22 / tablet | Play spoken test prompts aloud next to the S25 for physical-audio cases (adb via groot) |

The physical stratum (suite-v2) and the on-device models (Dessa doc slices B-E) start after the synthetic
loop's first plateau.

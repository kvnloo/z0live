# C1-r6 — V1 PASS(no-loss) BENEFIT-FAIL (r4 stays the default)

**Run (M).** One scored replay on GitHub Actions: https://github.com/kvnloo/z0live/actions/runs/46190662480 (event push, conclusion success, ubuntu-24.04, Python 3.12.16), branch `ci/c1-r6` @ `fb354822ac92333aa3279bd0a32dcf5ab4550672`. The job's `sha256sum -c FREEZE.txt` passed (PREREG, harness, fixtures, merged author text, author prompts/outputs, all vendored sources; `raw/artifact/c1-r6/freeze_check.txt`) and the harness re-checked the same manifest before scoring. The V1 block sha256 assert held (`e0ec860b…f220`, byte-identical to the r5 harness). No model, no network. One run only; no re-run, retune or exclusions. The push trigger was the scored run; `workflow_dispatch` was not used (the workflow file is not on the default branch and a second dispatch would have been a second scored run).

**Cohort r6 (M).** Seed 20261130, 17 synthetic scenarios (16 probe-bearing + 1 zero-probe control), 112 probes (85 non-digit, 27 digit current values), budget 2810. Scenario wording by free-pool LLMs given only the structural schema (not the V1 lexicon/code); fixture structure, kind mix and draws by the seeded script. 0 templates and 0 pool values dropped by the merge checks. Author attempts: p2 needed 4 tries, p1's N1 templates were unusable (literal names, 0/8 valid) and were redone once (see PREREG, `raw/author/*.failed*`). `codex` is not installed on this host, so the preferred author was unavailable.

## Primary gates (M)

| Arm | Warm losses vs r4 | Key-miss losses | Warm kept (stricter def.) | Trims L1/L2/L3 | Untrimmed lines | Sync key/pair | Gate |
|---|---:|---:|---:|---|---:|---|---|
| A exact r4 | ref: 112/112 present | ref | - | - | - | - | - |
| B0 unmodified sem-r3 (report only) | 8 (all non-digit; N7 x4, N3p x2, N2, N1; 7 scenarios) | 0 | 16/17 | 58 (9/45/4) | 3 | 0/0 | - |
| **B1 = sem-r3 + V1** | **0** | **0** | **16/17** (17th = zero-probe control) | 51 (5/45/1) | 10 | 0/0 | **PASS(no-loss)** |

Required: B1 warm losses 0, key-miss losses 0, warm kept >= 12/17; all met. Warm briefs over budget: 0. Added stale exposures (grader's per-probe measure): 0 in both states.

**Controls (M, all held; no NULL).** Known-loss control fired 16/16 probe-bearing scenarios. Key-miss output equals r4 in 17/17 (B0, B1). Sync key attempts 0 and pair attempts 0 in both states. Unguarded-miss control detected a synchronous key attempt in 17/17 (harness asserts). A/A, B/B warm and key-miss repeats: 0 flips (B0, B1; A repeat asserted byte-equal). Trim branch exercised (unmodified sem-r3) in 16/17 (gate >= 8). Trim exercised by B1 in 16/17. Trims that lost a labelled value (oracle, report only): B1 0, B0 8.

## Benefit metric (pre-registered; M)

Rule: median(len(A) - len(B1)) > 0 AND >= 10/16 scenarios with B1 smaller or fewer stale values exposed.

| Quantity | Value |
|---|---|
| median size reduction (chars, positive = B1 smaller) | **-58.5** (B1 larger) |
| mean size reduction | -78.3 |
| scenarios B1 smaller / larger | 1 / 15 (only r6/15, +12 chars) |
| total chars A / B1 (16 scenarios) | 41536 / 42789 (+3.0%) |
| scenarios with fewer / more stale values in B1 | 16 / 0 |
| total superseded values exposed A / B1 | 66 / 5 |
| scenarios "better" (rule) | 16/16 |
| **BENEFIT** | **BENEFIT-FAIL** (median < 0; the >= 10/16 half of the rule was met) |

Frozen verdict mapping: gate PASS(no-loss) + BENEFIT-FAIL = **`PASS(no-loss) BENEFIT-FAIL`**: r4 stays the default.

## Reading (I)

- Loss side replicates on an independent-author cohort: V1 0 losses vs r4 in both states (112 probes, 85 non-digit) while the unmodified sem-r3 loses 8 non-digit values (N7 shape most often, as in r5). B1 passes both holdouts. A sceptical point: on this cohort V1 mostly trimmed the harness's own fixed `assistant: Noted.` reply lines (46 of its 51 trims dropped it) and made exactly one level-3 trim (dropped "Actually"); it left 10 lines untrimmed. The author's filler phrasing mostly is not in the V1 lexicon, so V1 was safe by being inert on it, not by recognising it. That limits what the zero-loss result says about V1's trimming skill, and the loss gate was not stressed by many level-3 trims.
- The warm route did what it is for on staleness: superseded values exposed in the brief drop 66 -> 5 (all 16 scenarios). It did not make briefs smaller: B1 briefs are slightly larger than r4 in 15/16 scenarios (the two arms both fill toward the 2810 budget; why B1 fills more is not measured here). So the pre-registered size rule fails; the stale reduction is real but the rule fixed beforehand does not count it alone (the secondary line is informational per PREREG).
- Same pattern as the r5 dry replay noted in PREREG (larger briefs, 66 -> 2 stale), now on a cohort whose wording V1 had not seen: the stale benefit is stable, the size benefit is absent.

## Limits

One seed, one author run on a router of free models (identities vary per call); fixture structure, kind mix and probe asks follow my r5 design. Synthetic cache and keys; no live readiness, wall-time or answer-quality measurement, so "fewer stale values exposed" is not shown to improve answers. Stale count is a whole-token presence count of superseded values; it ignores where in the brief they sit. N7 had only 4 pairs this draw. B1's pass is partly inertness (above). Not production approval; no default changed.

Receipts: `PREREG.md`, `FREEZE.txt`, `c1_r6.py`, `fixtures.json`, `author_merged.json`(+`.log.json`), `author/`, `vendor/`, `summary.json` (per-scenario `details`, `benefit.per_scenario`), `analyze.py`, `raw/` (GHA artifact, job log, author raw outputs incl. failed attempts, analysis).

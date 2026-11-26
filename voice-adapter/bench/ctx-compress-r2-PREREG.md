# C1-ctx-compress-r2 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Rule, inputs and verdict script are frozen by sha256 in
`ctx-compress-r2-FREEZE.sha256` (checked in-workflow). Synthetic text only.

## Question
Does `compress_brief(brief, 2810)` keep planted facts that live in the brief region a tail cut would drop, once it
recognises lowercase speaker labels?

## The ONE change (production code)
`voice-adapter/brief_compress.py`: speaker labels are detected case-insensitively (`user:` / `assistant:` now get the
header / fact / filler path instead of the newest-chars tail fallback). The case-insensitive parse is used only when
the capitalised parse finds no speaker pair, so capitalised briefs are byte-identical (differential test
`test_brief_compress_r2.test_capitalised_path_byte_identical_to_c1` against the frozen pre-r2 copy
`bench/brief_compress_c1.py` on the slm-v2, e5, live, OTHER and capitalised context_eval briefs at 12 budgets).

## Harness fix (frozen before scoring; not the change under test)
C1 was non-discriminating: lowercase labels made compress_brief tail-cut, and `tiers.snapshot`'s 6000-char cap already
removed the stable brief and old chat turns from arm A. `bench/ctx_compress_r2_eval.py`:
- `suite()` = `context_eval.suite()` with chat noise shortened (noise(12) -> noise(6), noise(49+9i) -> noise(18+2i)).
  Stable brief, planted turns, call turns, probes and the scorer (`context_eval.score`) are unchanged.
- Full chat brief = 4741 / 5117 / 5491 chars (< 6000): every arm goes through the same `tiers.snapshot` (same cap,
  never binding), so arm A contains all six planted values.
- Brief-region facts: studio, locker (stable brief) and vendor (oldest chat turn). None is in the newest 2810 chars.
- Dry-run (M, offline) planted values present in context: A = studio, locker, vendor, flight, room, parcel;
  B (r2) = studio, locker, flight, room, parcel (vendor is a reply-less user turn, which compress_brief's fact rule
  scores as filler, and it is dropped at 2810); O (pre-r2) = flight, room, parcel.

## Arms and blocks
- A = full brief; B = r2 `compress_brief(brief, 2810)`; O = pre-r2 `compress_brief` (tail fallback), report-only.
- Per job: fresh llama-server blocks A1 B1 A2 B2 O1 on one runner (lanespeed.serve/ask: greedy, seed 42, max 120
  tokens, thinking off, cache_prompt false). 6 matrix jobs = 3 scenarios x probe halves (0-3 / 4-7).
- gemma-4-E2B-it Q4_K_M (sha256 740185b2...), llama.cpp b12527 prebuilt ubuntu arm64 CPU, ubuntu-24.04-arm.

## Primary metric and frozen rule (C1 rule, unchanged)
Planted recall per block = RECALL count over the 18 planted probes (3 scenarios x 6). Band = A/A flips (A1 vs A2).
**KEEP iff min(recall B1, B2) >= max(recall A1, A2) - band; else REVERT.**
NULL: a job missing/incomplete, a primary block missing, a request error in a primary block, scenarios != the three
workshops, or a primary block with != 18 planted probes. Applied once by `bench/ctx_compress_r2_verdict.py`.

## Report-only
- Discrimination check: O1 recall < min(A1, A2) (the test can see a dropped fact). If false, the test does not
  discriminate and a KEEP is not evidence of fact preservation.
- Brief-region recall (studio/locker/vendor) per block; B/B flips; first_sentence p50 pooled per arm (A, B, O).

## Expectation (I, stated before scoring)
If the model recalls what is in context, B loses vendor (3 probes) vs A; with band 0 that is REVERT. KEEP needs the
model to miss vendor in A too, or a band >= 3.

## Development dispatch (unscored)
One job (scenario 2 probes 4-7, blocks A,B) via `ctx-compress-r2-dev.yml` to qualify the prompt path and timeouts.
Not pooled, not scored. The scored workflow was also triggered by the first push (run 45905721230) and cancelled
during the GGUF download, before any bench step; not scored.

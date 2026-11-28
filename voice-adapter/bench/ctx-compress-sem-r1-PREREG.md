# C1-ctx-sem-r1 preregistration (track E, requirement C1/C2 context)

Written before the scored run. Inputs, harness, verdict script, workflow, the dry prediction, the precheck and this file are
frozen by sha256 in `ctx-compress-sem-r1-FREEZE.sha256` (checked in every abab job and in the verdict job). Synthetic text
only. Builds on C1-ctx-compress-r15 (PR #182, run 46080284422, sha 05c1b997): the blind set (vet / car / pottery) is r15's,
byte for byte. PR #183 (lane snapshot wiring) and PR #181 are not touched. `brief_compress.py` (r14) is untouched
(sha256 5c54ae42..., test-checked).

## Question
Does a cheap SEMANTIC supersession pass (one greedy model call per brief, same gemma-4-E2B-it Q4_K_M) supersede naturally
phrased updates that the lexical "my <subject> is" rules of r7-r14 never reached (0 of 9 on the blind set), without false
merges of still-true facts, without losing static facts, and at a pre-turn cost the voice path can afford?

## What failed in r7-r15, and what this changes
| round | outcome | what failed / was learned | change here |
|---|---|---|---|
| r8 (r7 rule) | REVERT | slot-only supersession merged two true facts | merging is the model's call per statement pair, still gated (G1, G1b, G1c) |
| r9 / r10 / r12 | REVERT | cue word or same head word on a different attribute dropped an older true fact | no cue word, head word or slot is read at all; a different-attribute merge is exactly what the model can get wrong, so it stays gated |
| r11 / r13 / r14 | KEEP | each fixed the previous round's experimenter-written limit | superseded as an approach: r15 showed the lexical surface (slot "my X is", cue word position, label) covers 0 of 9 blind updates |
| r15 | KEEP (narrow) | r14 = r4 byte for byte on the blind set; only the full brief answered 9/9 | the next lever is semantic (this round) |

## The one change (B arm): model-keyed supersession, `voice-adapter/brief_compress_sem.py`
Chosen mechanism (1), model-keyed, over (2) a pairwise judge. Reason (M, r15 jobs): CPU prefill is about 53 tokens/s
(1793 prompt tokens in 34 s) and decode about 17 tokens/s. A pairwise judge needs O(candidates x older) calls, each paying
its own prefill, so it cannot be cheaper than one batched call; the batched key pass pays one prefill of about 400 tokens
and about 100 generated tokens per brief.
- Candidates: fact exchanges (r4 definition: short reply or no reply) whose asker text holds a value word (a word with a
  digit); the statement is the first sentence holding such a word, at most 160 chars; at most 24 candidates.
- ONE request per brief to the block's own llama-server: temperature 0, seed 42, no prompt cache, thinking off,
  max_tokens = min(300, 16 x candidates + 16). The prompt (frozen, `brief_compress_sem.PROMPT`) asks for one short key per
  numbered line ("<number>: <key>"), the same key for lines giving a replacement value for the same code or number,
  different keys for different codes or numbers even about the same object. Its examples ("shed padlock code", "bus pass
  number", "warranty ticket") occur in no brief (test-checked) and it names no word of any set (test-checked).
- Keys are normalised (lowercase alphanumerics, STOP words my / the / a / new / old / current / updated removed).
- Candidates sharing a key are one group. Only the newest member stays; the older members are dropped and the newest is
  tried at the shortest size of the group (as r7). Unique or missing keys keep r4's key. Everything else is r4's order
  (fact order, filler spread, filler newest last).
- Fallback = r4 byte for byte: no keyer, HTTP error or timeout (180 s), no parsable key, no candidate, more than 24
  candidates, or a brief already within budget (test-checked on all 21 briefs plus degenerate inputs). A fallback in any B
  block fails G8 and is reported.
- Deterministic: greedy, seed 42, no prompt cache. The harness records per scenario and block the keys and the server
  timings; B1 vs B2 key disagreement is reported.
- Behind a function: `compress_brief_sem(text, budget, keyer=None)`. `brief_compress.compress_brief` and the r4 byte copy are
  untouched; A = `bench/brief_compress_r4.py` byte copy.

### Where the pass runs in the harness
`ctx_compress_sem_r1_eval.make_run_block` wraps the frozen r2 `run_block`. In a B block it first sends one unscored 1-token
warm-up request (every arm gets it), then runs the pass for every scenario of the block against the block's fresh server
(timed with `time.monotonic()` around the HTTP call), then the unchanged scored probes use the compressed brief. Outside a
block (dry, presence) B has no keyer and is r4. Each B row records `sem` (candidates, grouped, fallback, wall_s, server
timings, keys) and `in_brief` (planted value in the brief the block used); the verdict takes B's presence from `in_brief`.

## Design choices made with, and without, the blind set (disclosure)
- The r15 blind set was seen during r15 design. The semantic method is not tuned to its wording. The prompt was developed
  in three iterations using ONLY the main, recency, r8, r12, r13 and r14 sets (experimenter-written dev sets) and a local
  run of the pinned gguf; the r15 set was never sent to the model before the prompt was frozen:
  v1 ("the single thing whose value the line gives"): dev stale fixed 45 of 51, 1 false merge (r13 hotel a3_old);
  v2 ("name the value itself ... same key only if the later one replaces the very same code"): 0 groups on every brief
  (too strict); v3 (final, "the code or number that the line gives ... different codes or numbers get different keys, even
  about the same object"): dev stale fixed 44 of 51, 0 old facts dropped on any dev brief (M, local, Metal).
- The "newest at the group's shortest size" placement (r7) was compared with "newest at its own size" using a deterministic
  ORACLE keyer on the r15 set (no model): own size puts 0 of 9 updates in the brief (they sort behind the noise), so the r7
  placement was kept. That comparison did look at the blind set (disclosed).
- The oracle bound with the kept placement (M, deterministic, `precheck.json`): perfect keys give all 9 updates CURRENT and
  no stale value, pair-old 3/3, static 10/11: the car static fact s1 (licence ID) is displaced, because three newer
  statements (about 380 chars each) replace three older ones (about 260) in a brief that was already full. So **G1s is
  expected to FAIL on car even with perfect keys** (budget, not semantics). Kept as a gate as specified.

## Arms (all via `tiers.snapshot`, 6000 cap, budget 2810)
Model gemma-4-E2B-it Q4_K_M (sha 740185b2..., pinned), llama.cpp b12527 CPU, ubuntu-24.04-arm, greedy seed 42, fresh
llama-server per block, exactly the r15 workflow (24 jobs, same matrix and deadlines).
A = r4 byte copy; B = sem-r1; R = r14 (report-only, R1 on r15); O = full brief (report-only, O1 on main and r15).
Blocks: B A B A O (main); B A B A O R (r15); B A B A (dev).

## Metrics and gates (`bench/ctx_compress_sem_r1_verdict.py`)
Counts per primary block (A1 B1 A2 B2), summed over scenarios. band = max(A1 vs A2 flips, B1 vs B2 flips), pooled over the
103 scored r15 / r14 / r13 / r12 / recency / r8 probes per block. n: r15 9 updates, 3 pair-old, 2 pair-new (report), 11 static.
- **NULL:** as r15 (r2 verdict NULL; scenarios / incomplete job / missing primary block / request ERROR / category counts;
  no presence entry), plus a B row without `sem` / `in_brief`, a job without `sem_harness` meta, no semantic pass sample.
- **G1e r15_update_fixed (PRIMARY):** max(STALE_B) <= min(STALE_A) - 3 - band over the 9 updates, and min(CORRECT_B) >= 3.
- **G1 r15_two_true_loss (PRIMARY):** max(LOSS_B) <= max(LOSS_A) + band over the 3 pair-old facts.
- **G1s r15_static_recall:** min(RECALL_B) >= max(RECALL_A) - band over the 11 static facts.
- **Dev:** G1b r14 a3x_old a3y_old (6) and G1c r13 a2x/a2y + r12 a-d + r8 c_old (21): LOSS_B <= LOSS_A + band. G2 STALE no
  worse than A on r14 e3x e3y e1 e2, r13 e1 e2, r12 e1 e2, recency bench badge, r8 a1 a2: max(STALE_B) <= max(STALE_A) +
  band. G3 unique recall (r14 f, r13 f, r12 f, glaze, r8 d): min(RECALL_B) >= max(RECALL_A) - band.
- G4 main recall 18/18 via the frozen r2 verdict (KEEP); G5 r5 gates; G6 INVENTED on probes present in A or B: B <= A + band;
  G7 main first-sentence p50: B <= A + max(|p50 A1 - p50 A2|, 5% of A).
- **G8 sem_latency:** semantic pass p50 wall seconds per brief <= **3.0 s** on the GHA CPU runner, and no fallback in any
  B block. Sample = one distinct (job, B block, brief), about 54 expected. p95, max, mean, p50 per set, candidates and
  groups per brief are reported. The latency metric is its own number, separate from G7.
- KEEP iff every gate passes; otherwise REVERT, listing every failed gate. Report-only: r15 STALE / CORRECT by UPD_SAME /
  UPD_RENAMED and by slot form, pair-new recall, O1, R1, INVENTED absent-in-both, B1 vs B2 key disagreement.

## Deterministic precheck and dry prediction (written before the scored run; `precheck.json`, `dry-sem-r1.json`)
All from one local run of the same gguf and llama.cpp b12527 (macOS arm64 Metal build): (I) CI numerics may differ.
- **Does B group the 9 blind updates?** 7 of 9 (I): vet u1 (booking reference), car u1-u3, pottery u1-u3. Vet u2 (microchip
  "number" vs "ID") and u3 ("door code" vs "access code") get different keys: the model kept the noun.
- **Dev false merges?** None: 0 old facts dropped on 18 dev and main briefs. STOP rule: any dev old fact (r8 c_old, r12 a-d,
  r13 a2, r14 a3) that r4 holds and B drops => REVERT(design, precheck), no GHA run. Result: `stop = []`.
- **r15 false merge:** vet "My favorite vet tech is TECH-9" and "My go-to vet tech is TECH-12" get one key, so p_old is dropped
  (pair-old LOSS 1). Not a stop item (r15 is scored, not a dev set).
- **Dry prediction (I):** G1e PASS (B STALE 2 of 9, A 9; B CORRECT >= 3 if the model reads the newest value, O1 did 9/9),
  G1 FAIL (B 1, A 0), G1s FAIL (B 10/11: car s1, budget; also under perfect keys, M), G1b / G1c / G2 / G3 / G4 / G5 / G6 /
  G7 PASS, **G8 FAIL: about 11-15 s per brief** (about 370-450 prompt tokens / 53 tok/s plus about 100 generated tokens /
  17 tok/s, M rates from r15 jobs). Predicted outcome: **REVERT, failed G1, G1s, G8**. The local Metal wall time (0.5-0.9 s)
  is not the CI number.
- Mock verdicts (fake server + a heuristic stand-in keyer, real CLI path, `mock-smoke.py`): abstain and invent modes run
  end to end and give REVERT (the heuristic keyer false-merges on dev sets; it checks the pipeline, not the model).

## Limits
- n = 3 blind scenarios; smoke-scale, not powered. One blind author per topic (r15). One model, CPU only, greedy.
- Pair-old / static gates can fail through budget displacement or one model key choice; with band 0 one flip decides.
- A semantic pass costs pre-turn latency every time the brief exceeds the budget; the 3.0 s gate is the parent's budget.
- Production consumer: `compress_brief_sem` is not called by tiers.snapshot; PR #183 is untouched.
- Presence is a substring check; the r15 all-caps codes are not matched by the INVENTED pattern (as r15).

## Stop rule
- Exactly one scored run: the run started by the push that adds `.github/workflows/ctx-compress-sem-r1.yml` to
  `ci/ctx-sem-r1`. `gh workflow run` only if that push starts no run.
- A NULL from infra (GGUF download, runner loss, job timeout) may be re-run once at the identical commit
  (`gh run rerun --failed`).
- Any other change (set, prompt, rule, gate, harness, budget, model) is a new experiment (sem-r2).
- No edits to frozen files after the scored run starts.

# C1-ctx-sem-r6 calibrated-judge oracle: PREREG (written before any model call of this experiment)

Not a scored run. Question: does the UNCHANGED sem-r2 collision-judge prompt, decided by a calibrated REPLACE-vs-ALSO
first-token logprob margin with ONE threshold fitted on dev pairs only, fix the 2 clear false-ALSO verdicts of sem-r3
(v2-gym Tango-303/311, v2-pickup Route-12/15) without a new false-REPLACE on a clear still-true pair?

## The one change
Decision rule only. Prompt = sem-r2 `JUDGE_PROMPT` (unchanged), model gemma-4-E2B-it Q4_K_M (rev 0314792d), llama.cpp b12527,
ubuntu-24.04-arm CPU, temp 0, seed 42, max 4 tokens, same server flags. The request additionally asks for
`logprobs: true, top_logprobs: 50` (pre-sampling logprobs; does not alter generation).
margin = ln(P_replace) - ln(P_also) at the FIRST generated position, where P_group = sum of exp(logprob) over distinct
top-50 tokens whose string equals the first token of any variant of the group. Variants:
REPLACE: `REPLACE`, ` REPLACE`, `Replace`, ` Replace`, `replace`, ` replace`; ALSO: `ALSO`, ` ALSO`, `Also`, ` Also`, `also`, ` also`.
First-token strings come from the server's own /tokenize + /detokenize (ids and pieces are recorded in the artifact); if the two
groups share a first token the run aborts. A group absent from the top 50 gets probability floor 1e-12 (margin clipped at +-27.6).

## Pairs (frozen): `ctx_compress_sem_r5_pairs.json` (r5 file, same sha256 7413882b...), 198 pairs
Gold classes per r5: 65 clear update, 9 clear still-true, 2 ambiguous (report-only), 122 noise.
Sets present in the file: main, recency, r8, r12, r13, r14, r15, v2 (no v3 pairs exist in the file).
- DEV = main, recency, r8, r12, r13, r14 (non-blind sets). BLIND = r15, v2.
- Dev clear still-true: r8 bike/rack (id 188), r12 campsite (190), r12 coworking (191; the old prompt already says REPLACE).
  Blind clear still-true: v2 x6 incl. extra ISP account, r15 vet tech.
- Dev update pairs 45; blind update pairs 20 (includes both targets).

## Threshold t (fitted on DEV only, from pass 1)
Negatives = dev clear still-true + dev noise. Positives = dev clear updates.
m_neg = max margin over negatives. u = lowest dev-update margin strictly greater than m_neg.
t = (m_neg + u)/2 (midpoint of the only gap that keeps every dev negative ALSO and keeps the most dev updates REPLACE;
any t above m_neg and <= u gives the same dev REPLACE count, the midpoint is the widest-gap choice). Tie rule: dev updates with
margin <= m_neg cannot be separated and count as lost (reported); if no update is above m_neg then t = m_neg + 1.0.
Decision: REPLACE iff margin > t (strict); unparsable/missing logprobs -> floor margin, i.e. ALSO. t is applied unchanged to the
blind pairs; nothing is refitted or reclassified after the model has run.

## Procedure
One llama-server, one warm-up (unscored), tokenization check, then two margin passes (p1, p2) over all 198 pairs in file order.
A/A = number of pairs whose calibrated decision flips between p1 and p2, plus max |margin p1 - margin p2| (also verdict flips of the
raw reply). Decisions/t use p1. Also reported: p1 raw-reply verdicts vs the recorded sem-r3 verdicts (188 recorded pairs).

## Stop rule (frozen, same shape as r5)
PASS(oracle) iff all of: blind clear false-ALSO fixed >= 1 (clear update pair with sem-r3 verdict ALSO, calibrated decision
REPLACE; blind = r15, v2) AND new false-REPLACE == 0 (any blind clear still-true pair that the calibrated rule calls REPLACE while the
old p1 reply was not REPLACE) AND noise REPLACE (all 122 noise pairs, calibrated) <= old-prompt noise REPLACE count AND A/A
decision flips == 0. Otherwise NULL(oracle-stop): no scored run, no PR.
Also reported (not gating): new false-REPLACE over all sets (dev too), lost updates, AUC-style separation (P(margin_update >
margin_still_true) + 0.5 ties) over all clear updates vs clear still-true, margins of the 2 targets and 9 still-true pairs,
ambiguous pairs (report-only). Synthetic text only.

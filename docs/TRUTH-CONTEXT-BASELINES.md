# Truth and context baselines

Both evals run offline against a dedicated loopback llama-server. They do not use the live adapter, run lane races, or call hosted models. Track D reads the local voice traces. Track E generates its own conversation tape and never reads owner history.

## Reproduce

Use llama.cpp build `b12527` (`78e05298f`) with `gemma4-e4b.gguf` (Q4_0, SHA-256 `a555b900214b477d8880e7832e0b8925e139b0159640036b09fe472b6f2097f2`). Start a separate server on an unused loopback port. Do not reuse a server that forwards requests or logs prompts.

```sh
"$HOME/.local/opt/llama.cpp/b12527/llama-b12527/llama-server" \
  --model "$HOME/.local/share/z0live/models/gemma4-e4b.gguf" \
  --alias gemma4-e4b --host 127.0.0.1 --port 18087 \
  --gpu-layers all --ctx-size 8192 --parallel 1 --cache-ram 0 --jinja --no-ui --log-disable
```

From `voice-adapter/`, use the adapter virtualenv's Python. Both scripts write JSON to stdout. Keep the receipts local. The trace store changes during calls. Pass the same fixed trace files and check `cohort_sha256` when repeating a grading run.

```sh
umask 077
PY="$HOME/workspace/repos/z0live/voice-adapter/.venv/bin/python"
"$PY" bench/truth_eval.py --port 18087 > "$TMPDIR/z0live-truth-final.json"
"$PY" bench/context_eval.py --port 18087 --budget 300 --last-n 6 --repeats 5 > "$TMPDIR/z0live-context-final.json"
```

## Track D: recorded tier speech and must-SKIP

The scorer joins `lanes.spoken` to its preceding `lanes.tier` by trace file, delegation ID, tier, and model. It joins the lane reply to `delegation.start` and Hermes's `delegation.done` in that file. The denominator is lane answers marked for speech, not requests or all tier completions. The trace cannot prove an answer reached the caller's ear. The scorer excludes missing joins and text at the trace length caps.

Gemma returns one of `HALLUCINATION`, `CLEAN`, or `UNCERTAIN` under a fixed rubric. The report keeps `UNCERTAIN` and grading failures separate from clean answers. The all-answer rate treats confirmed positives as a lower bound under this rubric. The determinate rate excludes unresolved grades. Neither is verified ground truth. Hermes can be wrong or omit evidence a lane saw, and historical live state can change. Audit disputed labels locally before using the score to ship a gate.

Measured on 2026-11-20: 12 files contained 125 delegated requests and 154 lane speech marks. Four speech marks had no Hermes final. One pair reached a trace text cap. Gemma graded the remaining 149 replies: 12 flagged, 134 clean, and three uncertain. That is 8.1% flagged across all answers and 8.2% across determinate answers. The cohort hash is `6284a78773d46f62ab259ae1bcadd46b31bb3073d6030d53050ee55a42a2c971`.

Do not use 8.1% as a promotion threshold. The grader passed four of five synthetic controls. It falsely flagged a safe handoff as a hallucination. The command exits nonzero on that failure and still writes the receipt. Earlier rubric drafts also missed controls. A fixed local adjudication set is the next step before using this score as a gate.

Measured separately, local Gemma returned exact `SKIP` on all 8 of 8 slm-v1 must-SKIP prompts. No reply would have been spoken under the lane suppression rule. No spoken hallucination rate is defined for those eight prompts.

| Tier | Flagged / graded | Race median | Observed range | Race results |
|---|---:|---:|---:|---:|
| Fast | 3 / 44 | 2.037 s | 1.245 to 4.307 s | 45 |
| Medium | 6 / 50 | 2.033 s | 1.159 to 6.392 s | 53 |
| Slow | 3 / 55 | 2.891 s | 1.458 to 5.824 s | 59 |

Race latency comes from the `s` field of non-empty `lanes.tier` results, whether spoken or not. It measures elapsed time to the race result, not first token, audio, or grading latency. A future change must stay within 15% of the corresponding tier median on comparable inputs and retain answer coverage. Trace replay alone cannot establish that a new policy meets the latency gate.

## Track E: budgeted synthetic memory

Both arms use the same Gemma, production lane prompt, and questions. The snapshot arm uses `tiers.snapshot` without modification. The three-part arm keeps a stable brief, up to six recent call turns verbatim, and a blackboard whose latest update replaces the earlier value. It drops whole older recent turns first, then old blackboard entries if needed to fit the budget. The blackboard consumes annotations on the generated utterances, not the question answer key. This tests the layout if fact extraction is correct. It does not test extraction from unstructured chat.

Each arm answers 24 distinct questions over three workshop tapes. There are 18 planted slots, including six corrected slots, and six absent slots. The fixed scorer distinguishes correct values, superseded values, new unsupported values, abstentions, and off-format replies. Invented-fact rate covers single-slot values only, not extra claims in free-form answers. Context tokens come from Gemma's tokenizer, excluding lane instructions, the question, and chat-template overhead. The receipt also includes actual prompt usage from the completion server.

Measured on 2026-11-20 over five interleaved repeats. Each repeat produced the same outcome counts. The five repeats do not add independent scenarios.

| Metric | Current snapshot | Three-part context |
|---|---:|---:|
| Planted-fact recall | 15 / 90 (16.7%) | 90 / 90 (100%) |
| Superseded-fact errors | 0 / 30 | 0 / 30 |
| Invented facts | 0 / 120 | 0 / 120 |
| Correct SKIP on absent facts | 30 / 30 | 30 / 30 |
| Context tokens, median (range) | 2,686 (2,300 to 3,074) | 263 (263 to 263) |
| Tab warm TTFT, inferred from context token count | 4.91 s | 0.79 s |

The three-part layout cut median context tokens by 90.2%. In every repeat, the snapshot recalled only the three flight weekdays. It returned `SKIP` for the other 15 planted values, including the corrected room codes. The three-part arm retained all six recent turns and all three blackboard entries in each tape. Both arms completed all requests with no off-format replies. Server-reported full prompt lengths spanned 2,477 to 3,253 tokens for the snapshot and 440 to 442 for the three-part layout.

The TTFT row applies Track B's fit, `0.34 s + 0.0017 s × context tokens`. The snapshot median is beyond the measured fit range. The estimate excludes lane instructions, the question, and template overhead. Neither number is a phone measurement.

Run a local fact extractor against the same tapes next. Keep extraction accuracy separate from the answer scorer. Measure on the tab before claiming a phone latency gain.

# C1-ctx-compress prereg amendment 1 (infra fix; written before the rerun; the PREREG decision rule is unchanged)

Run 45892622241 (commit 91fb7dd) is NULL by the frozen rule: job workshop-3 hit the 2160 s deadline in block B2 after
4 of 8 probes (complete=false; wall 2196.5 s; A prefill ~3250 prompt tokens at ~79 s first sentence). workshop-1 and
workshop-2 finished (1594 s, 2002 s). The PREREG allows one infra fix + one rerun, then NULL.

Infra fix (sizing only): each matrix job now runs ONE scenario's probe half (probes 0-3 or 4-7, `--probes`), so 6 jobs
on 6 runners, each still ABAB A1 B1 A2 B2 with a fresh llama-server per block and one unscored warm-up. Per-job time
is about half (~17 min worst case). Arms, contexts, prompts, model, server flags, sampling, scorer and the KEEP/REVERT
rule are unchanged. A/A flips are still counted per (scenario, probe) between A1 and A2 of the same runner.

Verdict script changes (coverage checks only): scenarios are compared as a set (a scenario now spans two jobs), and
NULL also when any block label does not hold exactly 18 planted probes. Unit tests added for both and for --probes.

Not looked at before writing this: nothing to reveal a direction; the NULL run's per-block recall is reported in
RESULT.md as a separate (incomplete) receipt and is not pooled with the rerun.

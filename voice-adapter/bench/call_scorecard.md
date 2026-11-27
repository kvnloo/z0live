# Call scorecard

Run from the repository root with Python 3. The default input is the local voice
trace store. The output is newline-delimited JSON with one record per eligible
call and one roll-up per day. Date bounds are inclusive and use the trace
directory's local date. Explicitly bounded days with no matching calls get
zero-count summaries.

```sh
python3 voice-adapter/bench/call_scorecard.py --from 2026-11-19 --through 2026-11-27
python3 voice-adapter/bench/call_scorecard.py /path/to/day/trace.jsonl
```

The first trace row must be `call.created` with profile `tek`. Only counts,
latencies, and trace identifiers are emitted. The tool does not modify its inputs.
Use `--conductor`, `--lane-root`, or `--ledger-module` to point at another snapshot.

| Metric | Definition |
| --- | --- |
| Requests | Unique delegation IDs with a start event or an observed direct layout command. Caller turns are counted separately. |
| First spoken answer | Delegation start to the beginning of the first assistant turn whose transcript matches a speakable answer sent for that ID. Status messages are excluded. Provider turns are a proxy for phone playout. Unobserved answers include silence, missing telemetry, and paraphrases the matcher misses. Immediate voice-only answers cannot be tied to a delegation by this metric. |
| p50 and p90 | Nearest-rank percentiles, pooled from underlying observations for day records. Each distribution lists its sample size. |
| Tier SKIP rate | Recorded SKIP outcomes divided by recorded skips, errors, and winning answers. Racing attempts cancelled before emitting an outcome are outside the denominator. |
| Hermes route | Last observed route for each delegation, with request-to-terminal latency for completed or failed requests. The terminal sample count can be smaller than the route's request count. |
| Status volume | Receipt, progress, cap, and generic status events, alongside those matched to distinct provider turns. Rates divide by call minutes. Deferred and dropped columns count those events separately. A trace receipt line is a voice notification and has no authority as a ledger receipt. |
| Lane cards | Counts lane IDs in the voice's spoken and skipped trace events. The voice may combine several lane IDs into one spoken batch. |
| Lane evidence | A conductor-validated card counts as evidence that the lane answered. A zero-exit failure with a usable on-disk card is artifact-only. A zero-exit failure without one is empty. A nonzero, timed-out, or stopped lane counts as died. Starts without a terminal event are unfinished. The join uses the conductor call key and ask time. These observations reflect the available snapshot and do not establish that a card's recommendations were executed. |
| Action claims | Pattern-matched start and completion claims in Hermes answers and provider transcripts, counted separately. A comparable action item from the shared ledger needs a timely execution receipt for a start or a successful result for completion. Explicit lane claims may use matching conductor events. A Hermes background task receipt proves the background answerer ran. |
| Repeats within 10 minutes | Exact normalized text or high lexical overlap against an earlier request. Possible repeats require a shared content phrase and use a lower threshold. Caller-turn counts also cover utterances that produced no delegation. These signals can miss paraphrases, references such as “do that again,” and dropped transcript turns. |

A missing ledger module leaves claims checked against conductor evidence alone.
Claims flagged as unproven are candidates for review. Earlier work or evidence
from an external system may be absent from the trace and ledger.

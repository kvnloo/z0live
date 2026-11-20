# Speculative branching: answer before the caller finishes

Status: proposal (2026-11-20). Not built. Owner idea: "run multiple simulations in parallel using a couple fast
lanes, then coalesce on the optimal path."

## In one paragraph

While Kevin is still talking, two or three fast lanes each guess how his sentence will end and draft an answer to
that guess. The drafts sit ready. The moment he stops, z0live compares what he actually said with each guess, picks
the branch that matches, and hands that draft to the voice, which speaks it at once instead of "Sure, I'll check".
If nothing matches, it falls back to today's path: the tier race plus Hermes.

## Diagram

```mermaid
flowchart LR
  mic["Kevin's mic<br/>(S25)"] --> voice["Voice model<br/>GPT-Live / Grok / local"]
  mic --> asr["Live transcript<br/>(partial words)"]

  subgraph spec["While Kevin is still talking"]
    asr --> fork{"Fork at each<br/>pause > 150 ms"}
    fork --> b1["Branch A<br/>fast lane guesses ending<br/>+ drafts answer"]
    fork --> b2["Branch B<br/>another fast lane,<br/>different guess"]
    fork --> b3["Branch C<br/>no-guess: answer<br/>the words so far"]
  end

  subgraph pick["The moment Kevin stops"]
    eot["End of turn<br/>(VAD + semantic check)"] --> judge{"Coalesce:<br/>which branch matches<br/>what he actually said?"}
    b1 --> judge
    b2 --> judge
    b3 --> judge
  end

  asr --> eot
  judge -- "match" --> inject["Inject winning draft<br/>into the voice's context"]
  judge -- "no match" --> race["Fallback: today's tier race<br/>fast / medium / slow"]
  race --> inject
  inject --> voice
  voice --> spk["Kevin hears the answer"]

  ctx[("Shared context window<br/>chat brief · live transcript · blackboard")]
  ctx -.-> b1
  ctx -.-> b2
  ctx -.-> b3
  ctx -.-> race
  ctx -.-> hermes
  race --> hermes["Hermes agent<br/>(real work, lands last)"]
  hermes -- "adds something new" --> inject
```

## Timeline of one turn

```mermaid
sequenceDiagram
  participant K as Kevin
  participant Z as z0live
  participant F as Fast lanes (2-3)
  participant V as Voice
  K->>Z: "Can you find out how deep the Mari..."
  Z->>F: fork: guess endings, draft answers
  F-->>Z: draft A ("Mariana Trench": ~11 km)
  F-->>Z: draft B ("Marianas islands": ...)
  K->>Z: "...ana Trench is?" (stops)
  Z->>Z: match: A wins (B discarded)
  Z->>V: inject draft A
  V->>K: "About eleven kilometres, at Challenger Deep." (no filler)
  Z->>Z: tier race + Hermes continue; speak only if they add something
```

## Measured numbers this rests on (2026-11-19/20)

- Fast lanes answer 1.0-1.1 s after a hand-off (GPT-6 Luna, Haiku 5.5); local Gemma 4 E4B warm first token 0.04 s on
  the Studio; S25 cached lane prompt 0.18 s.
- Today the voice hands off ~1.5 s after Kevin stops, then says filler, then the answer (take 2: question ends 18.5 s,
  hand-off 20.1 s, lanes 21.4-22.3 s).
- Dessa takes its turn a median 0.43 s after the human stops.

## How it would be measured

Dessa replay (`voice-adapter/bench/dessa_run.sh`) for timing, plus two new numbers: filler rate (replies that open
with "I'll check"-type stalls) and branch hit rate (how often a speculative draft matched). Keep only if the turn gap
and filler rate drop and correctness (slm-v1-style grading of the spoken answer) does not.

## Advisor critique

(Fable advisor review pending; added below when it lands.)

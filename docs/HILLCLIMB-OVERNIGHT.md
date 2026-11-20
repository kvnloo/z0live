# Overnight hill-climbs (started 2026-11-19)

Kevin's ask: Dessa-level natural conversation with z0live on GPT-Live, Grok and a fully local voice (Liquid STT/TTS +
SLMs on GPU/NPU), local SLMs on the S22 and Lenovo tab, and z0live that does not depend on paid subscriptions:
"overcome compute limitations with intelligent timing, orchestration & parallelism ... think differently ... perhaps
there's a way to train a tiny model to be a god-tier orchestrator of all the other systems."

Two loops run as Hermes cron jobs in the poteto profile, each waking every 40-45 min. Each wake does ONE iteration:
read this file and the track's decision.tsv, take the lock, pick the top untried hypothesis, make one change,
measure with the frozen harness, run the gate, keep (commit) or revert (git checkout the files), log a row, release
the lock. A run that finds the lock held and fresh (< 90 min) exits without doing anything.

Rules for every iteration:
- One change, one measurement. Never stack untested changes. Never claim a win without the harness number.
- Never restart a Studio voice service during a call: `lsof -nP -iTCP:9168 -sTCP:ESTABLISHED` must print nothing and the
  newest trace in ~/.local/share/z0live/voice-traces must be older than 2 min. Kept changes deploy only then.
- Never force-push, never touch another profile's files, never delete user data on a phone.
- Commit kept changes on z0live `feat/lane-status`, staging only the changed files.
- Write a one-line entry to ~/.local/share/z0live/overnight.log at the end of every run (track, id, verdict, numbers).

## Track A: conversation (Studio, real calls)

Harness (frozen 2026-11-19): `voice-adapter/bench/dessa_run.sh LABEL [engines]`: the human side of 4 Dessa reference
calls x 60 s streamed into a real z0live call; AI-side timing scored by bench/dessa_timing.py.
Receipts: ~/.local/share/z0live/bench/dessa/. Decision log: ~/.local/share/z0live/bench/dessa/decision.tsv.

| | median gap | IQR | waits > 1 s | overlap starts | backchannels/min |
|---|---|---|---|---|---|
| Dessa (target, same clips) | 0.43 s | 0.01..0.73 | 13.2 % | 23.7 % | 3.5 |
| GPT-Live baseline | 1.30 s | 0.94..1.77 | 69 % | 13.8 % | 1.9 |
| Grok baseline | 1.17 s | 1.05..1.29 | 80 % | 12.0 % | 0.85 |

Score per engine: D = |median - 0.43| + max(0, waits>1s - 13.2)/100 + |backchannels/min - 3.5|/10
(lower is better; seconds-equivalent; every component is reported too). Keep a change when D drops by more than the
noise band (first iteration: re-run the baseline once to measure it) and no engine's D rises past noise, `spoke_first`
stays 0, the 65 adapter tests pass (`cd voice-adapter && Z0LIVE_DESKTOP=$PWD/../desktop env -u PYTHONPATH
.venv/bin/python -m unittest test_*` module names; without Z0LIVE_DESKTOP two lane tests fail on the groot default), and the replay WAVs show no garbled or doubled speech (listen to one per change:
spectrogram or ASR check).

Stop predicate: an engine is "Dessa-level" when D <= 0.15, median within 0.43 +- 0.15 s, waits > 1 s <= 20 %,
backchannels >= 2/min, held on two consecutive runs. The loop runs at least 10 iterations and continues until every
engine (GPT-Live, Grok, local) is Dessa-level or the backlog is exhausted. Then: Kevin's real S25 call is the final gate.

Fable advisor review (2026-11-19, ~/.local/share/z0live/advisor/plan-20261119.md, 2 consults) changed the order:
fix the bench before climbing. D alone rewards cutting the canned human off (a mid-turn interruption scores as a fast
floor-take) and 4 clips carry ~14 Dessa backchannels, so a single run cannot separate small wins from noise.

Hypothesis backlog (cheapest first; each names its mechanism):
0. BENCH FIRST (no engine change): (a) A/A repeat + bootstrap CIs on every component; (b) false cut-in rate: AI takes
   the floor in a pause where Dessa's own track stayed silent (the human track is identical, so Dessa's AI track labels
   which pauses were safe); (c) AI spurt durations, spurts < 0.5 s reported apart; (d) latency decomposition per Grok
   take: human end -> speech_stopped -> response.create -> first audio delta -> first voiced recorder frame (shows
   whether the 1.17 s is a server timer or our own path: 100 ms uplink batching, bridge, recorder). Rank on D AND false
   cut-in rate from then on. Also count delegation.created in the replay traces: near zero -> lane hypotheses do not
   move this bench.
   DONE 11-20 (A1): (a)-(c) and the delegation count are `bench/dessa_score.py RUN_DIR...` (CIs, false cut-in %,
   missed-take %, spurts < 0.5 s, delegations) and `--diff A B` (paired block-bootstrap dD). A/A repeat (`aa1`):
   Grok D 1.672 -> 1.654, dD -0.02 [-0.13, +0.14]: noise band ~0.15, single runs are enough. GPT-Live D 1.585 ->
   0.939 with no change, dD -0.65 [-1.23, -0.16]: run-to-run engine variance exceeds the within-run bootstrap, so a
   GPT-Live verdict needs >= 3 runs per arm compared by mean D; its A/A also spoke first once (anniversary), so
   `spoke_first` is a 1-in-4 noise event there, not a hard gate. Dessa scored on itself: D CI [0.05, 0.49], so D <= 0.15
   on 4 clips is at the bench's resolution limit. False cut-ins: GPT-Live 13.8 % both runs, Grok 20 % both; missed
   takes 34 % / 47 %. Delegations: 0 in all four runs -> lane hypotheses do not move this bench.
   DONE 11-20 (A2): (d) is `bench/dessa_latency.py RUN_DIR` over grok.timing trace marks (grok_leg.py). Run `a2-grok`
   (D 1.587, gap median 1.115 s), 19 of 26 takes fully matched, stage medians [IQR]: human end -> speech_stopped
   0.47 s [0.43, 0.50] (uplink + 100 ms batching + Grok's VAD silence window); speech_stopped -> response.created
   0.40 s [0.34, 0.44] (Grok commits the buffer ~0.34 s after speech_stopped, then responds); created -> first audio
   0.02 s; first audio -> voiced adapter frame 0.05 s; adapter frame -> recorder 0.20 s [0.196, 0.206] (WebRTC
   downlink + recorder; constant, so likely a fixed buffer, inferred). ~0.87 s of the 1.12 s is Grok's server turn
   timers: hypothesis 1 targets exactly that. The other 7 takes are later spurts of a response already playing (a pause
   inside one answer that dessa_timing scores as a new take). New: 1b. find the 0.20 s downlink constant (aiortc
   jitter/packetization vs the bench recorder) before trusting it as real-call latency.
1. Grok: turn detection owned by z0live. Server VAD with `create_response: false`; z0live calls `response.create`
   after a short silence (start 200 ms) instead of Grok's fixed ~1 s timer. Mechanism: the tight 1.05..1.29 IQR is a timer.
   REVERTED 11-20 (A3): xAI ignores `create_response: false` (pydantic-ai docs), so z0live cannot own the reply;
   the lever left is `silence_duration_ms`. 200 ms made it worse: human end -> speech_stopped 0.47 -> 0.72 s, D 1.587 ->
   1.971, dD +0.39 [+0.17, +0.63]. Grok's unset default is already shorter than 200 ms; hyp 2 sweeps downward (100/50).
2. Grok: sweep the silence threshold (300/200/120 ms) with a semantic veto (transcript ends mid-clause -> wait).
3. Speculative reply: request the reply at the first short pause, hold its audio until silence is confirmed, cancel if
   the human resumes (reversible playback). Mechanism: hides generation latency inside the pause.
4. Backchannels: during human turns > 3 s, at 150-400 ms micro-pauses with an incomplete clause, play a short
   pre-rendered "mm-hm"/"yeah" in the engine's own voice (cached clips), never over speech.
5. Brevity: cap spoken replies at 1-2 sentences in the persona (hosted voices over-talk); measure ai_talk_s too.
6. GPT-Live: persona backchannel policy "frequent" + "start answering as soon as the user is clearly done".
7. GPT-Live: drop or delay the z0live context items that arrive mid-turn (they can stall the response).
8. Fully local engine "local": LFM2.5-Audio-1.5B (Liquid; ASR + TTS in one model, llama.cpp runners for macOS and
   Android) for speech in/out, Silero/energy VAD + smart-turn, a fast local SLM (Gemma 4 E4B on Studio) for the reply,
   streaming TTS. First measure ASR and TTS latency alone; then wire it as an engine and run the harness.
9. Tiny orchestrator (see Track C): replace the hand-tuned timing rules with a learned policy once one beats them offline.
10. Real-call fix (not a bench item): a human "mm-hm" currently clears Grok's output (grok_leg.py speech_started); only
    clear on speech longer than ~300 ms or with words.

Expected ceilings (advisor, inferred): GPT-Live exposes no endpointer control, so without one its ceiling is ~10% of D
(backchannel clips only); Grok and the local engine are where Dessa timing is reachable. Only speculative replies held
in a buffer (hyp. 3) can produce Dessa's overlap starts.

## Track B: phone lanes (S22, Lenovo tab, S25; via groot adb)

Harness: `voice-adapter/bench/phone_bench.sh` (llama-bench, 1k-token prompt, 64-token reply) for raw speed, plus a
lane-latency probe to add first: llama-server on the phone, one warm request carrying the call brief, then 5 turns that
append ~50 new tokens each; metric = median time to first token of those turns (the real lane cost when the brief is
cached). Decision log: ~/.local/share/z0live/bench/phone/decision.tsv.

Measured 11-19 (1k prompt, no cache): S25 0.93-0.95 s (NPU Qwen3-1.7B / GPU Gemma 4 E2B); S22 CPU 15.6 s (GPU slower);
tab CPU 26.7-32.8 s; tab Vulkan (custom NDK build of b12527) segfaults right after init on Mali-G57 at every setting tried (B1). S22 Gemma GPU runs crash adb: skip them.

Hypothesis backlog:
1. Prefix cache: keep the brief in the KV cache (llama-server cache_prompt + a fixed slot). Expected S22 ~16 s -> < 1 s.
   PROBE 11-20 (B2): `bench/phone_lane_probe.sh SERIAL MODEL` (run on groot) is the lane-latency harness. S22 was off
   adb (wireless debugging off), so it ran on the tab, Qwen3-1.7B CPU: no-cache control 42.3 s -> warm median 2.80 s
   (5 turns, 23-25 new tokens each; cold brief 37.8 s / 1,168 tokens). TTFT == server prompt_ms, so adb adds ~0.
   Warm turns are bimodal 2.5 / 5.2 s and decode is 3.8 tok/s vs 9.5 in llama-bench: the 4 GB tab was swapping (1.6 of
   2.1 GB swap used, hre-slm's embed server also resident; inferred). Run the S22 when it is back on adb. The probe's
   server must stay in its own session (setsid): a wireless-adb drop kills adbd's children, hre-slm's server included.
2. Smaller lane prompt: compress persona + brief for phone lanes (a 300-token brief).
3. Threads and core pinning: -t 2..6, --cpu-mask big cores (S22: 1 X2 + 3 A710; tab: 2 A76).
4. Quantization: Q4_0 (ARM repack) vs Q4_K_M vs IQ4_NL; KV cache q8_0; flash attention on/off.
   REVERTED 11-20 (B4): `-ctk q8_0 -ctv q8_0` on the tab (qwen3-1.7b CPU, A-B-A): warm median 2.84 / 1.83 / 2.68 s
   (-33 %, no overlap) and 24-token turns 9.0 -> 5.4 s, but cold brief 36 -> 46 s and no-cache 42 -> 63 s. Failed the
   slm gate: usable 22/30 -> 20/30 (q8 says "30" for 15 % of 80, SKIPs Ottawa and a C->F conversion). Next: V-only
   q8_0 (`-ctv q8_0`), since K is usually the quantization-sensitive half (guess).
5. Smaller models for the slow phones: Qwen3-0.6B, Gemma 4 E2B where it runs.
6. Speculative decoding on the S25 (0.6B draft for 1.7B).
7. Tab GPU, later: debug build + tombstone for the Vulkan crash, or a Mali-native runtime (MLC-LLM / MNN OpenCL).
8. Role split: S22/tab as background workers (summaries, prefetch, embeddings), S25 as the live fast lane.
Gate: slm-v1 usable score must not drop more than 1/30 for a model/quant change (run slm_bench.py on the Studio with the
same GGUF); phones must stay cool (thermal status < 3) and on adb.

## Track C: the tiny orchestrator (research, inside Track A's loop once A's first rules land)

Advisor scope cut: realistic as FLOOR CONTROL only (hold / backchannel / take floor / release speculative / flush), not
lane routing (no labels; tiers.race already routes). A 1-5M-parameter causal GRU head on frozen features (Silero +
smart-turn embeddings, ASR partial length, own-speaking state, ready-buffer state, time since human end) on the voice
host CPU; ~1,200 Dessa floor-takes are too few to train from scratch. Labels come free from the two-track audio (human
audio -> Dessa's action). Offline eval: leave-one-call-out, policies compared on predicted D and false cut-in rate,
plus an ORACLE that triggers at the true turn end (if even the oracle misses, speculative replies move up). Keep an
acoustic-timeout fallback. First experiment: the offline script on the 20 downloaded pairs (no live calls needed).


A small model (or gradient-boosted policy first) that, every 50-100 ms, reads cheap features (VAD state, pause length,
partial transcript tail, prosody, which lanes have answers ready, engine latency estimates) and emits: wait / backchannel
/ take the floor now / start speculative reply / cancel / which lane's answer to speak. Training data we can generate:
the 34 Dessa two-track files (when Dessa and the humans took the floor, backchanneled, overlapped), our own replay
traces, and synthetic dialogs. Offline eval first: replay the Dessa human tracks through the policy alone and score
the chosen floor-take times with dessa_timing; it touches a call only after it beats the hand rules offline.
The Fable advisor's plan (~/.local/share/z0live/advisor/plan-20261119.json) refines this.

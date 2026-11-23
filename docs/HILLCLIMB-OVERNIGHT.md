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
- Data privacy (docs/REQUIREMENTS.md, owner rule 11-20): call traces may go to the providers that already saw them;
  Telegram, chat history and owner-msgs stay local (local models only for inference, grading and training).
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
.venv/bin/python -m unittest test_*` module names; without Z0LIVE_DESKTOP two lane tests fail on the the desktop host default), and the replay WAVs show no garbled or doubled speech (listen to one per change:
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
   DONE 11-20 (A5, 1b): the replay now writes NAME-clock.json (sender, recorder onsets on the trace's clock) and
   dessa_latency reports the one-way legs for any engine. The 0.20 s was a round trip: the alignment fit on input
   onsets absorbs the uplink, so out_frame->heard = uplink 0.12 s [0.09, 0.19] + downlink 0.09 s [0.08, 0.10] (both
   in-process aiortc loopback, not network; a phone's stack will differ). Bigger find: the recorder concatenated
   frames and lost 0.14-0.39 s of underruns per 70 s call, so the AI WAV drifted early (0 at the start, -0.2..-0.3 s
   by 60 s; median -0.135 s). Every gap scored before A5 is biased short and overlap/false cut-in biased high, worst
   late in a clip. Fixed (playout-buffer placement: never before arrival, underruns written as silence): placement
   -0.003 s [-0.011, +0.007], no drift. Pre-A5 D values are not comparable; first fixed-bench GPT-Live run `a5fix` D
   1.423 (median 1.14 s, waits > 1 s 64.5 %, bc 1.5/min, spoke_first 1). Re-baseline both engines (>= 3 GPT-Live runs)
   before the next engine change. Grok's xAI client_secrets returned HTTP 403 at 03:35 (worked at 02:53).
   DONE 11-20 (A6, GPT-Live re-baseline on the fixed bench): 3 runs (a5fix, a6rb1, a6rb2) D 1.423 / 1.535 / 1.153,
   mean 1.370, sd 0.20; median gap mean 1.12 s (1.14 / 1.215 / 1.015), waits > 1 s 58.8 %, bc 1.29/min, false
   cut-in 18.6 % (16.1 / 14.7 / 25.0), missed take 30.7 %, spoke_first 0 in both new runs, delegations 0-1. With sd
   0.20 a 3-vs-3 comparison resolves only a mean-D drop of about 0.3 (2 x SE of the difference, 0.16): GPT-Live
   changes worth less than that need 5+ runs per arm. The GPT-Live baseline for later rows is mean D 1.370. Grok
   still gets xAI client_secrets HTTP 403 at 04:30 (all 4 calls), so it has no fixed-bench baseline and every Grok
   hypothesis is blocked until Kevin restores the xAI login (Hermes `xai` auth; not a bench fault).
1. Grok: turn detection owned by z0live. Server VAD with `create_response: false`; z0live calls `response.create`
   after a short silence (start 200 ms) instead of Grok's fixed ~1 s timer. Mechanism: the tight 1.05..1.29 IQR is a timer.
   REVERTED 11-20 (A3): xAI ignores `create_response: false` (pydantic-ai docs), so z0live cannot own the reply;
   the lever left is `silence_duration_ms`. 200 ms made it worse: human end -> speech_stopped 0.47 -> 0.72 s, D 1.587 ->
   1.971, dD +0.39 [+0.17, +0.63]. Grok's unset default is already shorter than 200 ms; hyp 2 sweeps downward (100/50).
2. Grok: sweep the silence threshold (300/200/120 ms) with a semantic veto (transcript ends mid-clause -> wait).
   REVERTED 11-20 (A4): 50 ms gave D 1.587 -> 1.743, dD +0.16 [-0.04, +0.39]; human end -> speech_stopped 0.58 s
   vs 0.47 unset and 0.72 at 200 ms. Any explicit value is slower than leaving it unset, and speech_stopped ->
   response.created stays ~0.39 s whatever the window. Closed: the silence window is not a lever. Grok's ~0.87 s of
   server timers can only be hidden (hyp 3, speculative reply) or filled (hyp 4, backchannels), not tuned.
3. Speculative reply: request the reply at the first short pause, hold its audio until silence is confirmed, cancel if
   the human resumes (reversible playback). Mechanism: hides generation latency inside the pause.
4. Backchannels: during human turns > 3 s, at 150-400 ms micro-pauses with an incomplete clause, play a short
   pre-rendered "mm-hm"/"yeah" in the engine's own voice (cached clips), never over speech.
   REVERTED 11-20 (A10): GPT-Live's own "Mm-h" (cut from a8g1 by `bench/bc_clips.py`) played by the output
   AudioBridge after >= 3 s of caller talk, at 40 ms of caller quiet, voice silent, 6 s cooldown (tuned offline with
   `bench/bc_sim.py`). 3 runs (a10bc1-3) D 0.905 / 1.162 / 0.868, mean 0.978 vs A8 1.029 (-0.05, noise; sd 0.20);
   bc 2.21 -> 2.35/min, median gap 0.98 -> 0.96 s, false cut-in 18.0 -> 18.8 %, spoke_first 0/12; tests 68 ok.
   Clips play clean (`bench/bc_check.py`: correlation 0.98 with the asset, none doubled), but of 18 found in the WAVs
   only 6 landed as backchannels and 12 as floor-takes: by the time a pause-triggered clip reaches the ear (uplink
   ~0.12 s + 40 ms + adapter -> recorder ~0.13 s) the 180 ms pause bridge has closed, so the "mm-hm" lands after the
   human stopped. Those count as fast floor-takes (overlap starts 26.5 % in two runs): exactly the D gaming the advisor
   warned about. The first sim had no pipeline delay and overstated the gain ~3x; bc_sim's `--delay 0.29` now
   reproduces the live mix. Lesson: a clip triggered by a pause it must land inside cannot work over a ~0.3 s path.
   Next variant (4b): launch the clip during speech, predicted ~0.3 s ahead (energy/pitch fall, clause-final ASR
   partial), and score it in bc_sim with the delay before any live run.
5. Brevity: cap spoken replies at 1-2 sentences in the persona (hosted voices over-talk); measure ai_talk_s too.
   REVERTED 11-20 (A9): GPT-Live persona "one or two short sentences per reply unless the user asks for more". 3 runs
   (a9b1-3) D 1.212 / 1.134 / 1.138, mean 1.161 vs A8's 1.029 (+0.13, inside the 0.3 resolution, wrong direction);
   ai_talk_s 64.8 vs 59.9 (the cap did not shorten talk), bc 2.21 -> 1.36/min (the main D loss), median gap 0.98 ->
   0.99 s, waits > 1 s 48.3 -> 51.5 %, false cut-in 18.0 -> 18.5 %, missed take 28.1 -> 30.7 %, spoke_first 0/12.
   A length rule competes with the backchannel sentence and wins. Closed for GPT-Live.
6. GPT-Live: persona backchannel policy "frequent" + "start answering as soon as the user is clearly done".
   KEPT 11-20 (A7, 952dca6, deployed ~05:39): three persona sentences (acknowledge longer stories with "mm-hm"/"yeah",
   answer as soon as the user is clearly done). 3 runs (a7p1-3) D 0.881 / 1.129 / 0.980, mean 0.997 vs 1.370
   (-0.37, past the 0.3 resolution; Welch t ~2.7, df ~3.4, so marginal: confirm with 2 more runs per arm when
   cheap). Median gap 1.12 -> 0.94 s, waits > 1 s 58.8 -> 45.6 %, bc 1.29 -> 1.85/min, false cut-in 18.6 -> 15.9 %,
   missed take 30.7 % both, ai_talk_s 72 -> 56 s, spurts < 0.5 s 23 -> 26 per run. Trace transcripts show the new
   backchannels are real words inside the human's story ("Mm-hm.", "Yeah?"), not blips. spoke_first 3/12 calls vs
   2/12 baseline: in every case an "Mm-h"/noise token at t = 1.6 s, before any human audio, and baseline has it
   too (pre-existing; new item 11). context_probe on the deployed persona: all_pass (delegates "look at my screen").
11. GPT-Live speaks a lone "Mm-hm" (or a noise token) ~1.6 s into 1 in 5 replay calls, before the human says anything.
    Find the trigger (context append, lead-in silence, or the recorder's first frames) before it reaches a real call.
    KEPT 11-20 (A8, 879f816 + 66ad1f1, deployed ~07:10): the provider itself opens an assistant turn at its own
    t = 400 ms while the persona context lands (context.appended 200..1200 ms), in 8 of 36 pre-A8 replay calls
    (3/4 in a7p2; transcripts " Mm-h", " [sn", " He Hey"); the energy check flagged only some. It already reached a real
    call (11-19 21:05, "He Hey" before Kevin spoke). Not a recorder artifact. Fix: AudioBridge zeroes output audio and
    the adapter drops output transcripts until the phone sends sound (Level onset) or the provider transcribes caller
    speech. 3 runs (a8g1-3): 3 early provider turns in 12 calls, all silenced, spoke_first 0/12; D 0.869 / 1.057 /
    1.162, mean 1.029 vs 0.997 (+0.03, noise; sd 0.20); false cut-in 18.0 %, missed take 28.1 %. context_probe 6/6
    all_pass (the first probe, before the transcript half, failed on a gated "Yeah," still sent as text). Open: the
    gated turn still lands in the call's exchange log; a greeting-first design would need a different gate.
7. GPT-Live: drop or delay the z0live context items that arrive mid-turn (they can stall the response).
   NOT ON THIS BENCH (A9 check): a8g1's provider tx are only the start-of-call context appends (t 0.48..0.69 s),
   no mid-turn items, delegations 0-1 per run. Only real calls with lanes/delegation can test it.
8. Fully local engine "local": LFM2.5-Audio-1.5B (Liquid; ASR + TTS in one model, llama.cpp runners for macOS and
   Android) for speech in/out, Silero/energy VAD + smart-turn, a fast local SLM (Gemma 4 E4B on Studio) for the reply,
   streaming TTS. First measure ASR and TTS latency alone; then wire it as an engine and run the harness.
9. Tiny orchestrator (see Track C): replace the hand-tuned timing rules with a learned policy once one beats them offline.
10. Real-call fix (not a bench item): a human "mm-hm" currently clears Grok's output (grok_leg.py speech_started); only
    clear on speech longer than ~300 ms or with words.

Expected ceilings (advisor, inferred): GPT-Live exposes no endpointer control, so without one its ceiling is ~10% of D
(backchannel clips only); Grok and the local engine are where Dessa timing is reachable. Only speculative replies held
in a buffer (hyp. 3) can produce Dessa's overlap starts.

### Fully local Dessa replay

`bench/local_voice_replay.py` reuses the four frozen 60 s human clips, the 2 s lead-in, the 8 s tail,
and the existing WAV scorer. It paces 20 ms input frames on the same clock as recorded output. The energy
VAD uses a −43 dBFS threshold and a 440 ms silence window. A confirmed human onset cancels the reply and
cuts any PCM that has not played. Liquid LFM2.5-Audio Q4_0 runs ASR and TTS in separate processes. Gemma 4
E4B generates one short reply before Liquid streams PCM. The benchmark starts only offline loopback servers.
It does not add a production engine or use WebRTC, device playout, smart-turn, or an explicit backchannel policy.

| Measured on the Studio | Median gap | Overlap starts | Waits > 1 s | Backchannels/min | D |
|---|---:|---:|---:|---:|---:|
| Dessa target, same clips | 0.43 s | 23.7% | 13.2% | 3.50 | 0.000 |
| GPT-Live A6 baseline, mean of 3 runs | 1.123 s | 23.8% | 58.8% | 1.28 | 1.371 |
| Local, 1 run | 1.065 s | 0.0% | 64.7% | 0.21 | 1.479 |

The local run completed all four clips with no model errors or early speech. It scored 34 floor-takes,
with a gap IQR of 0.992 to 1.090 s, 11.8% false cut-ins, and 21.1% missed takes. It played 17 complete
replies, canceled 28 on human onset, and cut one at clip end. The within-run block bootstrap put D at
1.251 to 1.657 (95% interval). GPT-Live's historical run range was 1.153 to 1.535. The local arm omits
WebRTC buffering and is not a matched phone path. One local run does not establish a speed win or a D regression.
It did not reach the Dessa gate and seldom backchanneled.

Measured stage medians for the 17 complete local replies: 442 ms from last voiced input to ASR submission,
116 ms for final ASR, 240 ms for the full Gemma reply, 44 ms to first TTS PCM, and 253 ms from first PCM
to the first sample above −50 dBFS. The endpoint wait was the largest measured stage. The first PCM was not
yet voiced audio. The earlier Liquid-only 77 ms ASR and 25 ms first-PCM numbers measure isolated requests on
synthetic clips (`docs/VOICE-LOCAL-BASELINE-20261120.md` on `origin/feat/voicebase`, `baf1c50`).

The private replay receipt is `~/.local/share/z0live/bench/dessa/localvoice-final-1795198489-r1-local.json`.
The same-named directory holds WAVs and per-turn timings. `bench/dessa_score.py` reads those WAVs for the
bootstrap and the cut-in and missed-take rates. The GPT-Live row uses saved `a5fix`, `a6rb1`, and `a6rb2`
receipts. No hosted rerun was made.

Run from `voice-adapter/` on the Studio with `ffmpeg`, `say`, and the installed GGUFs. Use a fresh label and directory.

```sh
export DESSA_PYTHON=../../z0live/voice-adapter/.venv/bin/python
bash bench/dessa_run.sh my-local-run local
umask 077
env -u PYTHONPATH "$DESSA_PYTHON" bench/local_voice_replay.py --slm \
  --out "$HOME/.local/share/z0live/bench/my-local-slm-run"
```

`--slm` speaks the 30 unchanged slm-v1 questions with macOS `say`, then runs the same VAD, ASR, Gemma, and TTS
path. It supplies the frozen brief and any prior turns as text. It scores the output WAV by transcribing it with
Liquid ASR and applying the suite regexes. A model `SKIP` becomes a spoken deferral. Both text and spoken scores
are recorded. The spoken score is a same-model ASR proxy, not a listening test or a natural-speech accuracy estimate.
The private reference tracks, output WAVs, and transcripts stay outside Git.

Measured on one full spoken run: 27/30 correct from the output WAV, versus 28/30 on Gemma's text.
The spoken breakdown was facts 4/6, reasoning 5/6, brief context 6/6, follow-ups 4/4, and deferrals 8/8.
All 30 cases played an answer, with no execution errors. Private receipts are at
`~/.local/share/z0live/bench/localvoice-final-1795198489-spoken/`.

## Track B: phone lanes (S22, Lenovo tab, S25; via the desktop host adb)

Harness: `voice-adapter/bench/phone_bench.sh` (llama-bench, 1k-token prompt, 64-token reply) for raw speed, plus a
lane-latency probe to add first: llama-server on the phone, one warm request carrying the call brief, then 5 turns that
append ~50 new tokens each; metric = median time to first token of those turns (the real lane cost when the brief is
cached). Decision log: ~/.local/share/z0live/bench/phone/decision.tsv.

Measured 11-19 (1k prompt, no cache): S25 0.93-0.95 s (NPU Qwen3-1.7B / GPU Gemma 4 E2B); S22 CPU 15.6 s (GPU slower);
tab CPU 26.7-32.8 s; tab Vulkan (custom NDK build of b12527) segfaults right after init on Mali-G57 at every setting tried (B1). S22 Gemma GPU runs crash adb: skip them.

Hypothesis backlog:
1. Prefix cache: keep the brief in the KV cache (llama-server cache_prompt + a fixed slot). Expected S22 ~16 s -> < 1 s.
   PROBE 11-20 (B2): `bench/phone_lane_probe.sh SERIAL MODEL` (run on the desktop host) is the lane-latency harness. S22 was off
   adb (wireless debugging off), so it ran on the tab, Qwen3-1.7B CPU: no-cache control 42.3 s -> warm median 2.80 s
   (5 turns, 23-25 new tokens each; cold brief 37.8 s / 1,168 tokens). TTFT == server prompt_ms, so adb adds ~0.
   Warm turns are bimodal 2.5 / 5.2 s and decode is 3.8 tok/s vs 9.5 in llama-bench: the 4 GB tab was swapping (1.6 of
   2.1 GB swap used, hre-slm's embed server also resident; inferred). Run the S22 when it is back on adb. The probe's
   server must stay in its own session (setsid): a wireless-adb drop kills adbd's children, hre-slm's server included.
   KEPT 11-20 (B7, measurement): S22 still off, S25 back on the desktop host adb, so hyp 1 ran on the S25 (qwen3-1.7b, android
   build, `-dev GPUOpenCL -ngl 99`, KEEP_MODEL=0): no-cache control 2.92 s (1,437 tok) -> warm median 0.18 s
   [0.166..0.181], cold brief 2.33 s / 1,144 tok, thermal 0. The cache makes the S25 a sub-200 ms lane once the brief
   is warm, so the brief should be prefilled at call start, before the first question (next: wire it, hyp 8 role split).
2. Smaller lane prompt: compress persona + brief for phone lanes (a 300-token brief).
   KEPT 11-20 (B10, measurement + probe knob `BRIEF_CHARS`, default 4300 = the frozen 1,144-token brief): tab,
   qwen3-1.7b CPU `-t 6`, A-B (arm then fresh control). 304-token brief: cold 36.3 -> 7.5 s (-79 %), warm median
   2.44 -> 1.10 s (-55 %, no overlap: arm 1.03..1.30, control 2.16..4.80), no-cache 37.7 -> 13.5 s. Warm turns grow
   with the cached context (1.03 s at 351 cached tokens -> 1.29 s at 540), so every token of brief is paid on every
   turn, not only once; the tab's swap pressure likely adds to the 1,144-token arm (inferred). The tab becomes a
   ~1 s warm lane with a short brief. Open: a 300-token brief carries fewer facts; before wiring it, run a
   planted-fact check (does the compressed brief still answer the chat-context asks in slm-v1?). S22 still offline.
   KEPT 11-20 (B11, measurement): what a lane really gets is `tiers.snapshot(context_text)`, the raw chat tail capped
   at 6,000 chars (mobile_live.py), plus tiers.SYSTEM, not the call brief. At that size (`BRIEF_CHARS=6800`, 1,784
   tokens) the tab (qwen3-1.7b CPU `-t 6`) runs cold 66.2 s, warm median 3.59 s [3.49..3.75], no-cache 67.0 s, vs a
   fresh 1,144-token control: cold 36.1 s, warm 2.67 s [2.47..2.84], no-cache 49.2 s. Across B10/B11 a warm
   24-token turn costs ~0.34 s + ~1.7 ms per cached token (304 -> 1.10 s, 1,144 -> 2.67 s, 1,784 -> 3.59 s), so the
   cache saves the prefill but every turn still pays attention over the whole cached context. A phone lane at today's
   context size is not a live lane on the tab. It needs its own context budget (about 300 tokens for ~1 s warm), so
   the planted-fact check of a compressed lane context is the gate before wiring any slow-phone lane.
   KEPT 11-20 (B12, planted-fact check; S22 offline 10 h, S25 off adb, Studio only): `bench/slm-v2.json` = slm-v1's
   30 cases with its facts planted in a 5,545-char synthetic chat tail (real lane size, general-knowledge filler; no
   owner data). `bench/lane_compress.py` derives a short-brief suite: `tail` (newest 1,100 chars) or `model` (a local
   GGUF rewrites the tail as Kevin's facts). qwen3-1.7b (the tab's GGUF), usable / context asks: v1 brief 22/30, 6/6
   (reproduces B9's control); v2 full tail 21/30, 6/6; v2 tail-1100 16/30, 1/6 (truncation drops 5 of 6 facts);
   v2 qwen3.5-9b compressed (579 chars, ~150 tokens inferred, 3.75 s on the Studio, all 11 facts kept verbatim)
   20/30, 6/6, skip 8/8, no invented facts. Passes the 1/30 gate at the limit, but general facts fell 4/6 -> 1/6:
   behind a bare fact list the 1.7B SKIPs "capital of Canada" and "symbol for gold" (lookup-only framing, inferred).
   So model compression, not truncation, is the way to a ~300-token phone lane context. Next: render the compressed
   facts as chat-style sentences (or say in the lane header that general knowledge is still answered) and re-score
   v2-model; then the tab warm TTFT at that size (B10 fit predicts ~0.9 s, inferred). Run the probe
   from a file on the desktop host (`scp`, then `bash /tmp/probe.sh ... < /dev/null`). Piped over `ssh bash -s` it does nothing,
   because `adb shell` eats the rest of the script from stdin. The first B11 attempt dropped the tab's wireless adb at
   78 s cold. That killed hre-slm's :8081 embed supervisor, which was restored with `hre_slm_server.sh` and the args
   in `docs/fleet/hre-phone-sweep.md` (k3s-hre-phone-sweep worktree).
   REVERTED 11-20 (B13, Studio, same GGUFs): a header over the compressed facts ("Kevin's own facts ... general
   questions are still answered as usual") does not undo the over-SKIP. qwen3-1.7b 20 -> 19/30 (fails vs v2 full 21):
   it recovers f-gold but still SKIPs f-capital and now SKIPs r-order. gemma4-e2b (first v2 rows): v2 full 25/30,
   compressed list 24/30, header 25/30, so Gemma keeps facts and general knowledge behind a short brief, but it fails
   must-SKIP 2-4 of 8, including a false action claim ("I will text Harsha that you are running ten minutes late").
   The B12 list (20/30, at the gate limit) stays the qwen3-1.7b short brief.
3. Threads and core pinning: -t 2..6, --cpu-mask big cores (S22: 1 X2 + 3 A710; tab: 2 A76).
   KEPT 11-20 (B6): `-t 6` on the tab (qwen3-1.7b CPU, A-B-A vs -t 4): warm median 2.72 / 2.78 -> 2.39 s (-13 %),
   cold brief 37.1 / 34.7 -> 30.9 s (-14 %), no-cache 41.2 / 45.8 -> 36.9 s. Args only, no model change (slm gate
   n/a), thermal 0. The second control's first two turns hit swap (6.7, 4.4 s; SwapFree 415 MB after). Tab lanes run
   `-t 6`; the probe default stays -t 4 so earlier rows compare. Next: `-t 8` (all cores, competes with hre-slm).
   REVERTED 11-20 (B8): `-t 8` vs `-t 6` on the tab (A-B-A): warm median 2.39 / 2.42 -> 2.24 s (-7 %), but the turns
   overlap (Mann-Whitney U 36/50, p ~0.18 normal approx.; arm [2.99, 2.24, 2.09, 2.09, 2.29], controls 2.17..2.48), cold brief
   30.2 / 32.6 -> 28.7 s, no-cache 35.4 / 35.8 -> 37.7 s. Within noise, and all 8 cores would starve hre-slm's embed
   server in real use. Threads are closed on the tab at `-t 6`.
4. Quantization: Q4_0 (ARM repack) vs Q4_K_M vs IQ4_NL; KV cache q8_0; flash attention on/off.
   REVERTED 11-20 (B4): `-ctk q8_0 -ctv q8_0` on the tab (qwen3-1.7b CPU, A-B-A): warm median 2.84 / 1.83 / 2.68 s
   (-33 %, no overlap) and 24-token turns 9.0 -> 5.4 s, but cold brief 36 -> 46 s and no-cache 42 -> 63 s. Failed the
   slm gate: usable 22/30 -> 20/30 (q8 says "30" for 15 % of 80, SKIPs Ottawa and a C->F conversion). Next: V-only
   q8_0 (`-ctv q8_0`), since K is usually the quantization-sensitive half (guess).
   REVERTED 11-20 (B5): `-ctv q8_0` alone on the tab (A-B-A): warm median 2.76 / 2.70 -> 2.43 s (-11 %), but cold
   brief 35.8 -> 65.7 s and no-cache 43 -> 85 s (V-quant prefill is ~2x slower on this CPU, inferred from the cold and
   no-cache rows). slm gate 22 -> 21/30 (passes), and the one flip is the same "30" for 15 % of 80: the percent
   error comes from V, not K. Every call pays a fresh brief, so +30 s cold outweighs -0.3 s per warm turn. KV quant
   is closed on the tab; the cold brief is the cost to attack (hyp 2, smaller brief; hyp 5, smaller model).
5. Smaller models for the slow phones: Qwen3-0.6B, Gemma 4 E2B where it runs.
   REVERTED 11-20 (B9, slm gate): Qwen3-0.6B Q4_K_M on the tab (`-t 6`, vs a fresh 1.7B `-t 6` control): warm
   median 2.41 -> 1.84 s (-24 %, no overlap: arm 1.83..2.09, control 2.28..2.44), cold brief 35.8 -> 16.6 s (-54 %),
   no-cache 35.6 -> 16.6 s. Fails the gate on the same GGUF: slm-v1 usable 22 -> 18/30, and the losses are the
   dangerous kind: it answers 5 of 8 must-SKIP asks, 3 with invented personal facts ("Your downloads folder is currently
   96 percent full", "cloudy with light rain", "Nothing new in my email"). Not a speaking lane. Halving the cold brief
   by model size costs too much; attack the brief itself (hyp 2) or prefill it before the first ask (hyp 8).
6. Speculative decoding on the S25 (0.6B draft for 1.7B).
7. Tab GPU, later: debug build + tombstone for the Vulkan crash, or a Mali-native runtime (MLC-LLM / MNN OpenCL).
   REVERTED 11-20 (B14; the patch is kept as a tool): the B1 crash was three null core-1.2 entry points. Driver r32p1
   exposes a Vulkan 1.1 device under a 1.3 loader, and ggml checks only the instance version.
   `bench/ggml-vulkan-mali-vk11.patch` (gates bufferDeviceAddress, falls back to vkGetDeviceQueue, and enables
   VK_KHR_timeline_semaphore) makes b12527 run. qwen3-1.7b, llama-bench pp256/tg32 on the idle tab: Vulkan 4.00 / 2.79 tok/s
   vs the generic CPU build 38.23 / 9.96 (prefill 9.6x slower). Mali-G57 MC2 is a 2-core GPU, so tuning cannot beat
   the CPU (inferred). gemma4-e2b with -ngl 99 (2.84 GB pinned on a 3.9 GB device) took the tab down mid-run: no adb,
   no hre-slm :8081, no open TCP port afterwards (likely a reboot, inferred). Never offload Gemma on the tab. A
   foreground app (com.kvnloo.rehit, 117 % CPU + surfaceflinger 49 %) skewed the first runs: check `top` before a tab run.
   Tab GPU is closed. A Mali-native runtime is only worth trying if a model needs more than the CPU gives.
8. Role split: S22/tab as background workers (summaries, prefetch, embeddings), S25 as the live fast lane.
   KEPT 11-20 (B15, measurement, call-start prefill): in 13 real call traces the first lane ask (delegation.start)
   came 22.1 s after call start at the earliest (median 39.7 s, n 12), and asks were 55 s apart (median, min 4.3).
   The tab's cold prefill (B10/B11) is 7.5 s for 304 tokens, 36 s for 1,144 and 66 s for 1,784. Prefilling at call
   start therefore makes the first ask warm in 12/12 calls with the short brief, 8/12 at 1,144 tokens and 4/12 at the
   real lane context (inferred from both measured sets). Later turns already reuse the prefix (cache_prompt). The probe
   has a lane mode for this: `BRIEF_FILE=<lane system text>` sends tiers.SYSTEM + snapshot through the model's own
   chat template (any model, e.g. Gemma). The real lane context is 1,478 tokens; on the Studio warm turns reuse
   1,464-1,603 tokens and add 37-61 each. Best tab lane today: qwen3-1.7b Q4_K_M, generic CPU build, `-t 6`, f16 KV,
   B12's ~150-token compressed facts, prefilled at call start: warm ~1.1 s (B10, 304-token brief), cold 7.5 s hidden
   before the first ask, slm-v2 20/30 vs 21/30 for the full context (skip 8/8). Not measured: gemma4-e2b on the tab
   CPU (tab offline since B14).
Gate: slm-v1 usable score must not drop more than 1/30 for a model/quant change (run slm_bench.py on the Studio with the
same GGUF); phones must stay cool (thermal status < 3) and on adb.

## Track D: answer truthfulness (hallucination rate)  -- job z0live-climb-D-quality, alternates with Track E

Harness to build first (iteration 1): `voice-adapter/bench/truth_eval.py`. Cases: (a) recorded call turns that were
delegated, with Hermes's final answer as reference (call traces may go to the providers that saw them, see
REQUIREMENTS.md); (b) slm-v1 must-SKIP cases (personal, live, action). Metric: hallucination rate = spoken lane answers
that contradict the reference or invent personal/live facts or claim actions, graded by a fixed grader model; also
SKIP-correctness. Gate: lane latency (tiers race median) must not rise > 15%.
Backlog: 1. disagreement rule: fast vs slow lanes contradict on a fact -> voice defers ("let me check") instead of
picking; 2. personal facts must cite the brief/blackboard entry; unsourced personal claims are dropped; 3. pre-speak
gate for kinds math/hard/gather on in tiers mode (it may not run there today); 4. per-model blocklist from slm-v1
harmful answers; 5. confidence phrasing ("I think") when only one fast lane answered.

## Track E: context relevance and compression (planted-fact recall)  -- same job, alternating

Harness to build first: `voice-adapter/bench/context_eval.py`. Build long synthetic contexts (no private data:
synthetic chat, or call traces only) with planted facts and planted corrections ("flight moved to Thursday"); feed the
compressed context a lane would get; ask about each planted fact. Metrics: planted-fact recall, superseded-fact error
rate (answers with the old value), invented-fact rate, context tokens (smaller is better), phone lane TTFT from Track B's
measured slope (tab: ~0.34 s + 1.7 ms per cached token).
Backlog: 1. budgeted three-part context (stable brief / last N turns verbatim / blackboard); 2. rolling summary of older
turns by a LOCAL model; 3. fact extraction to blackboard entries with source + time, corrections overwrite; 4. per-turn
retrieval with local embeddings instead of stuffing; 5. stable prefix + append-only for prefix caching.
Private history (Telegram, chat, owner-msgs) never goes to hosted models in this track: local models only.

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

DONE 11-20 (C1, branch `research/orchestrator`, offline only, nothing deployed): hand rules vs learned floor policies.
Tools in `voice-adapter/bench/`: `floor_data.py` (causal 20 ms human-audio features and Dessa's action per frame: 20
calls, 71.7 min, 616 takes, 186 backchannels), `floor_asr.py` (LFM2.5-Audio on the Studio CPU transcribes the 983 human
talkspurts, 98 % non-empty, ~3 min), `floor_eval.py` (closed-loop replay: a take plays Dessa's own reply at the
policy's onset, a backchannel plays a 0.4 s clip, both scored by dessa_score; leave-one-call-out, parameters picked on
the other 19 calls by J = D + false cut-in %/100; delay 0.25 s = A5 uplink + A10 adapter->recorder, ready reply),
`floor_traces.py` (decision points from real call traces, timings only). Every run asserts the identity check: Dessa's
own timing through the simulator gives back dessa_timing's numbers exactly. Private outputs: `~/.local/share/z0live/orch/`.

| measured, 20 calls, LOCO | J | D | false cut-in | missed take | takes only (`--no-bc`): J, false cut-in, missed |
|---|---:|---:|---:|---:|---|
| Dessa itself | 0.228 | 0.228 | 0 % | 0 % | 0.137, 0 %, 0 % |
| deployed rules (0.3 s Level quiet + A10 clips) | 0.340 | 0.110 | 23.0 % | 14.6 % | 0.222, 21.2 %, 14.9 % |
| rules tuned per fold | 0.262 | 0.041 | 22.1 % | 17.7 % | 0.243, 22.3 %, 14.6 % |
| gate: pause classifier + timeout fallback | 0.212 | 0.016 | 19.5 % | 20.3 % | 0.189, 15.9 %, 21.6 % |
| gate+text | 0.226 | 0.024 | 20.2 % | 16.2 % | 0.151, 15.1 %, 20.8 % |
| hazard logistic / gbm (audio) | 0.231 / 0.369 | | | | 0.249 / 0.203 |
| hazard logistic / gbm (+text) | 0.219 / 0.210 | | | | 0.194 / 0.195 |

Verdict. With backchannels in play, no learned policy beats the tuned rules past noise (gate dJ [-0.147, +0.012]).
Every policy, the tuned rules included, beats the deployed constants (gate dJ [-0.213, -0.064]). On the take decision
alone the learned gate wins. gate+text vs the deployed rules: dJ [-0.150, -0.022], false cut-ins 21.2 -> 15.1 %
([-9.0, -3.2] points). vs the tuned rules: dJ [-0.114, -0.038]. It pays about 6 points more missed takes
(14.9 -> 20.8 %), which J does not charge. Six learned variants were tried, so the nominal 95 % CIs are slightly
optimistic. Why (all measured):
(1) D is gameable. A tuned timeout parks its takes at 0.2 s of quiet + 0.25 s = 0.45 s and scores D 0.04, better than
Dessa's 0.23, with 22 % false cut-ins.
(2) dessa_timing counts any AI talkspurt inside a human one as a backchannel, so replies the human talks over earn D's
backchannel term: the deployed rule scores 2.8 "backchannels"/min with no clips at all. Take-only runs drop that term.
(3) Pause-triggered clips land after the human stopped (A10 again): 14-52 % of each policy's false cut-ins are clips.
(4) At 0.2 s of quiet the audio barely separates turn ends from holds: the hazard models reach pause AUC 0.59 and the
best feature (end-of-run pitch) 0.64. Words help: the pause classifier rises from 0.60 to 0.70, and an open last word
("and", "the", "um") ends 32 % of holds but 16 % of turn ends. Frame hazards learn when to fire, not whether; the gate
uses the signal.
(5) The oracle (true turn ends, ready reply) has median gap 0.26 s, so with a ready reply the floor is turn-end
certainty, not endpoint speed. Its 6.6 % false cut-ins are the canned human's floor.
Real calls (`floor_traces.py`, 11 GPT-Live calls, 160 min, Level spans on the adapter clock): GPT-Live's gap median is
1.51 s with 65 % over 1 s. Kevin pauses longer than the Dessa humans on the same Level rule: hold-pause median 0.81 s
vs 0.48 s, and a 1.0 s timeout cuts 40 % of his holds vs 4.5 % of theirs. A Dessa-trained gate would cut into him far
more than offline (inferred). Next: label Kevin's own pauses (trace shifts/holds plus local transcripts) and refit the
gate on them before any live arm. Backchannels need the in-speech trigger (4b), not a pause trigger.

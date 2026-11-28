# Pre-text GPU warm-keep (design, not implemented)

Status: **design only, default OFF when implemented.** This repo has no CUDA-backed local TTS path today.
Local TTS here is either an HTTP call to a Liquid server (`voice-adapter/bench/local_voice_replay.py`, `LocalModels.tts`)
or an MLX helper process (`bench/lure_tts_helper.py`). Neither owns a CUDA device in-process, so there is no place
for a pre-text hook and none was invented. This document records the evidence and the hook spec for whoever adds a CUDA engine.

## Evidence (measured on a bench, one Linux host with an RTX 3080 Ti; not production)
Modeled pre-text window of 300 ms in which the engine runs fp16 2048x2048 matmuls to keep the GPU out of low-power clocks, stopped when text arrives.

| experiment | metric | result |
|---|---|---|
| K-P2W (preregistered) | warm (P2) TTFA, paired median warm-keep minus none | -13.66 ms (boot95 -14.33..-12.91), 39/40 pairs negative, sign test p=3.7e-11, A/A band 6.58 ms |
| K-P2Wt (time-bounded loop) | same, replicated | -13.92 ms |
| both | PCM output | bit-identical (100/100) |

Downside (measured): some COLD calls got worse. The first CUDA launch after idle can stall on the host (page faults / memory pressure,
142-618 ms, 20-148 major faults). In K-P2W all 10 warm-keep COLD calls over 200 ms had a warm loop that overran text arrival
(154-653 ms), versus 5 over 200 ms for the baseline arm. A time-bounded loop (K-P2Wt) did not fix it, because the stall is inside the first launch,
where a bound cannot act. Hence the gate below.

## Hook spec
1. Flag `TTS_WARMKEEP` (env or config), **default off**. Off means the warm callable is never invoked.
2. Armed when the reply-generation phase starts (the window before the first text delta). Disarmed at text arrival.
3. Run only if ALL hold, else skip and log one line with the reason (`warmkeep skipped: <reason>`):
   - device is CUDA;
   - platform is Linux with readable `/proc/pressure/memory` and `/proc/pressure/io`; any missing file or non-Linux host means no-op;
   - `some avg10` of both files is at or below `TTS_WARMKEEP_PSI_MAX` (suggested start 1.0 percent);
   - `MemAvailable` in `/proc/meminfo` is at or above `TTS_WARMKEEP_MEM_FLOOR_MB` (suggested start 4096).
4. The warm work is chunked (e.g. a few ms per matmul batch), checks a stop event between chunks, and must stop at text arrival.
   It must never delay the real first launch: on text arrival set the event and do not wait on in-flight warm kernels beyond one chunk.
5. Never run during barge-in teardown or while a turn is being cancelled.
6. Pure-Python gate (`should_warm(flag, device, psi_reader, meminfo_reader) -> (bool, reason)`) with injectable readers so it unit-tests without a GPU.

Tests to write with the implementation: flag off never calls warm; flag on and low pressure calls warm and stops at text arrival;
high PSI or low MemAvailable skips with a logged reason; non-Linux or non-CUDA is a no-op; no warm during barge-in teardown.

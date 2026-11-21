# z0live on the Mac Studio

The Studio (`studio`, tailnet `100.100.77.46`) runs the whole voice stack natively under launchd. groot's
copy keeps working from its own tree; nothing here depends on groot.

## What runs

| LaunchAgent | Process | Listens | Job |
|---|---|---|---|
| `com.zer0.z0live.core` | `bin/z0live serve` | 127.0.0.1:8791 | Broker and lanes config: presets, spoken layouts, mid-call edits |
| `com.zer0.z0live.hermes` | `desktop/hermes_host.py` | 127.0.0.1:8644 | API-only Hermes host the core's lanes call |
| `com.zer0.z0live.voice` | `voice-adapter/mobile_live.py` | 127.0.0.1:9168 | Voice slot A |
| `com.zer0.z0live.voice-next` | `voice-adapter/mobile_live.py` | 127.0.0.1:9169 | Voice slot B |
| `com.zer0.z0live.proxy` | `voice-adapter/dashboard_proxy.py` | 127.0.0.1:9165 | Voice-live HTTP requests go to the active slot. Other traffic goes to the dashboard on :9119. |
| `com.zer0.z0live.lanes` | `voice-adapter/lane_server.py` | 127.0.0.1:8646 | Plain model calls for the adapter's model lanes |
| `com.zer0.z0live.gemma` (optional) | `llama-server` | 127.0.0.1:8650 | Local Gemma 4 E4B inference |

LaunchAgents load at login and log to `~/Library/Logs/z0live/<label>.log`. The active voice slot restarts after a crash. The inactive slot stays stopped between rollouts.

Call path: Hermes app → Tailscale HTTPS → proxy → adapter → GPT-Live (`gpt-live-1-codex`) over
the Codex ChatGPT login. The `poteto` profile hands delegated work to the local Hermes API host.
Lane turns go adapter → core `/v1/lanes/turn` with the relay token.

## Install or repair

Wait until no Live Voice call is active. From the deployed checkout of the approved revision, run:

```sh
Z0LIVE_PRIMARY_HOST_PROFILE=poteto ./deploy/macos/install.sh
```

The installer builds `voice-adapter/.venv` from `requirements.lock`, writes the LaunchAgent plists,
pairs the relay token once, and re-registers the jobs. It sets `HERMES_HOME` for
`com.zer0.z0live.hermes` to `$HOME/.hermes/profiles/poteto` and sets `Z0LIVE_PRIMARY_HOST_PROFILE`
for `com.zer0.z0live.voice` to match. Use the installer rather than `kickstart` alone, which does not
reload changed plist environment variables. The separate Tek host is not managed by this installer.

After install, check that ports 8644 and 9168 listen on loopback. Send an authenticated request to
`/p/poteto/v1/models` on port 8644 using the existing core API credential. Expect HTTP 200.
Then run an in-process primary hand-off against port 8644 and check `delegation.api` and
`delegation.done` for the same delegation ID. Do not set the probe-only provider cap in launchd.

Run `install.sh` only in an idle maintenance window. It builds `voice-adapter/.venv`, pairs the relay token once, and re-registers the LaunchAgents, including the proxy. The first install of rolling restart switches the proxy to managed routing and starts with slot A. Do not rerun `install.sh` for routine adapter releases.

## State and secrets

- Data: `~/.local/share/z0live/` (`core/` broker DB and `.hermes-api-key`, `voice-traces/`, `call-briefs/`, `handoff.json`). Mode 700.
- Relay token: `~/.config/z0live/hermes-mobile-relay.token` (0600). The core stores only its SHA-256. Lost: `z0live revoke` the old device, delete the file, rerun install.
- Optional `~/.config/z0live/voice.env` (0600, bws-synced), read by `run.sh`. `GROQ_API_KEY` there turns on the Groq instant lane; without it the instant lane is warm Haiku.
- adb: `~/.local/bin/adb` (Google platform-tools). Phone needs Wireless debugging on.

## Change the lanes live

Say it on the call, or type it in any chat that forwards to the call: "gimme 2 fast lanes, 1 large, no
council", "make the large lanes Opus", "switch to preset B". The core parses it (`lanes_intent.py`), applies
it to this call only and the voice confirms in one sentence. Presets live in `desktop/z0live_desktop/lanes.py`;
the default layout file is `$Z0INT_HOME/config/z0live-lanes.json` (`z0live.lanes.v1`, validated by `lanes_v1.py`).

## Front door (one app URL, every call traced)

The Hermes app uses `https://studio.taila30dc.ts.net:9119` (tailnet only). Tailscale serve holds the
certificate and forwards to the z0live proxy on `127.0.0.1:9165`, which sends the voice-live paths to the
active voice slot (`:9168` or `:9169`). The adapter writes `~/.local/share/z0live/voice-traces/<day>/*.jsonl`. The proxy sends everything else to
the dashboard on `127.0.0.1:9119`. This is groot's layout (`https://0.taila30dc.ts.net:9119`). The proxy tells
the dashboard the app is on https (`HERMES_PROXY_FORWARDED_PROTO`), so session cookies are Secure; behind a
plain-http front door they are never sent back and every ws-ticket fails with `no_cookie`. Without it the app talks to
the dashboard directly, calls run on stock Hermes voice (no lanes, no brief, no trace), and the only sign of
a call is `ws_ticket_minted` lines in `~/.hermes/logs/dashboard-auth.log` (what happened on 11-19).

The dashboard therefore binds loopback: in `~/Library/LaunchAgents/com.kvn.hermes-dashboard.plist` use
`hermes dashboard --host 127.0.0.1 --port 9119`, restart it (`launchctl kickstart -k
gui/$(id -u)/com.kvn.hermes-dashboard`; not during a call), then run `deploy/macos/install.sh`. Check:
`tailscale serve status` lists `:9119 -> http://127.0.0.1:9165`, and every z0live port and the dashboard
listen on 127.0.0.1 only.
Read a call: `voice-adapter/trace_report.py <trace.jsonl>`.

## Roll out an adapter release without dropping open calls

One command: `deploy/macos/release.sh [commit]` builds `~/.local/share/z0live/releases/<sha>` (detached worktree and
venv) and runs the rolling restart below. Safe while a call is open. `deploy-when-idle.sh` is only for `--install`.

After the idle-window install, prepare each adapter release in its own checkout or extracted directory. Build `voice-adapter/.venv` there with the Studio Python and install that release's `voice-adapter/requirements.lock`. Keep the source and venv unchanged while a slot points to that directory. Use a release that includes managed adapter startup and `/_z0live/ready`.

From the installed control checkout, run:

```sh
./deploy/macos/rolling-restart.sh /absolute/path/to/prepared-release
```

The script starts the inactive slot and verifies that its loopback HTTP readiness response comes from the expected LaunchAgent PID. This check does not open a model session. It then updates `~/.local/share/z0live/voice-slots/route.json` atomically. The proxy stays up and sends new session offers to the new slot. Status and diagnostics also follow the active slot, so proxy diagnostics no longer show calls that are draining on the old slot.

Requests already forwarded to the old slot finish signaling before it receives `SIGTERM`. Open calls stay on their original WebRTC peers. The old adapter exits when those calls end or when `DRAIN_SECONDS` expires. A drain timeout can still end a call. The script never uses `kickstart -k` or sends `SIGKILL`.

- If startup fails, new calls stay on the active slot. Check the candidate's log in `~/Library/Logs/z0live/`. Fix the staged release and rerun with the same directory.
- If the script reports that the old slot is still draining, the cutover has already happened. Run `./deploy/macos/rolling-restart.sh` without arguments to finish. A new rollout cannot start until the previous slot exits.
- To roll back a completed cutover, run another rollout with the previous release directory. Keep both directories until the rollback window ends. Do not edit `route.json` by hand or run `install.sh` while a call is open.
- Voice configuration applies to the next call. It does not need a restart.

### Isolated verification

From `voice-adapter/` in a prepared checkout, run:

```sh
Z0LIVE_DESKTOP=$PWD/../desktop env -u PYTHONPATH .venv/bin/python -m unittest -v test_rolling_restart
```

The probe runs the deployer, proxy, and both adapters on temporary loopback ports. Two synthetic WebRTC calls use a local fixture for authentication and provider signaling. It checks that the first call still carries media while draining, the second call goes to the new slot, and the old adapter exits after the first call hangs up. It does not exercise launchd or hosted model readiness.

### Level 2 design: keep media stable and reload the brain

Level 1 keeps the old release running for existing calls. Level 2 would let an open call adopt new conversation logic without renegotiating WebRTC.

- A long-lived media holder owns the phone and provider transports, data channels, audio routing, and call lifecycle. It keeps a bounded local event buffer and remains responsible for mute and hangup while a worker changes. Restarting this holder still ends its calls.
- A reloadable brain worker owns conversation policy, lane dispatch, and delegation. The holder and worker exchange versioned messages over a private Unix socket. Tag messages with call and turn IDs, sequence numbers, and a worker generation. Keep private chat and Telegram data local, and replay only the state the new worker needs.
- Warm a candidate worker and check schema compatibility before switching. At a turn boundary, checkpoint the old worker, replay events after that checkpoint, then switch command ownership to the new generation. Discard stale commands and deduplicate side effects by action ID. If the candidate fails before the switch, keep the old worker. If the new worker fails afterward, keep audio and hangup working and reattach a compatible worker from the latest checkpoint.

Prove the design by swapping workers mid-call on isolated ports. Check continuous audio, unchanged peer connections, ordered turns, and no repeated delegation. Repeat with a rejected checkpoint and a worker crash before and after the switch. Use Level 1 to roll out changes to the media holder itself.

## Call trace events

- `context.sent` records what the adapter sent to the voice, not what the model read.
  `turn_id` is a call-local caller turn number. `0` is call setup. Delayed delegation
  answers keep the requesting turn's ID. `leg` identifies the primary or guest voice.
  The row includes the wire `type`: `session.update` for Grok instructions,
  `signaling.session` for GPT-Live setup instructions, `conversation.item.create`
  for Grok text and tool results, or the GPT-Live context append types.
  Instruction rows contain `instructions_sha256` and `instructions_bytes` over UTF-8.
  Other rows contain the exact `texts` plus `channel` and delegation `did` when applicable.
- `user.reaction` records four booleans per finalized caller turn. `barge_in` uses
  caller audio onset and recent non-silent audio sent to the phone. It estimates
  overlap at the adapter, not at the phone speaker. `repeat` compares token sets
  against the previous three caller turns at Jaccard similarity ≥ 0.6.
  `correction` matches English cues such as "no,", "I meant", and "wait", plus
  Tamil cues such as `இல்ல` or `illa`, `நான் சொன்னது` or `naan sonnadhu`, and
  `காத்திரு` or `kaathiru`. `frustration` matches cues such as "bro", "already told",
  and "what?", plus `டேய்` or `dei` and `ஏற்கனவே சொன்னேன்` or `erkanave sonnen`.
  The phrase table matches Tamil script and case-insensitive romanized forms in the
  same turn. It does not transliterate arbitrary text. These labels are heuristics,
  not judgments about intent.

`chunks()` budgets context appends in UTF-8 bytes, not characters or model tokens.
The byte cap is 500. Tamil-script code points take three bytes each, so the cap
fits at most 166 of them in a chunk with no other characters. The local test
measures encoded bytes and checks that joining chunks recovers the input. The token
bound is inferred from the byte cap, not measured with the provider's tokenizer.

`Z0LIVE_TRACE_AUDIO=1` writes a 16 kHz mono PCM WAV beside the JSONL when caller
frames arrive. It records only caller audio and writes silence for adapter-muted
frames. It is off by default. `HERMES_MOBILE_VOICE_TRACE=0` disables both files.
These files stay local by default. Share call trace data only with providers that
already saw it. Keep Telegram, chat history, and owner messages local.

## Model lanes (fast, medium, slow)

When the voice hands a request to Hermes, three lane tiers start at once beside Hermes's own agent turn:
fast = Haiku 5.5 + gpt-6-luna, medium = Sonnet 5.5 + gpt-6.1-terra, slow = Opus 5.5 + gpt-6.2-sol
(`voice-adapter/tiers.py`; the newest of each tier in the Codex catalog for a ChatGPT account).
Models race within a tier. Every lane gets the same context: the chat brief plus the call's recent turns.
The first real answer is spoken; a later tier, and Hermes's own answer, speak only if they add something
(at most three spoken parts per request). Lanes go quiet once a newer request starts or Hermes answers.
Lanes reply SKIP for phone, screen, file or action requests, which leaves those to Hermes.

`lane_server.py` (LaunchAgent `com.zer0.z0live.lanes`, 127.0.0.1:8646) makes plain model calls through the
poteto profile's logins: the Claude subscription, and a separate Hermes Codex login for GPT
(`HERMES_HOME=~/.hermes/profiles/poteto hermes auth add openai-codex`). Don't adopt `~/.codex`: GPT-Live uses
that token family, and sharing it logs one of them out. Change the models per call without a restart by
writing `~/.config/z0live/tiers.json` in the shape of `tiers.TIERS`. `Z0LIVE_TIERS=off` in the voice plist
restores the older instant/Fable/relay lanes.

Check one request end to end (real GPT-Live, synthetic speech, real lanes, Hermes faked after 20 s):
`PROBE_HERMES_DELAY=20 PROBE_ASK="Can you find out how deep the Mariana Trench is?" Z0LIVE_TIERS=on ... context_probe.py`;
the receipt's `spoken` lists when each part was said, and the trace has `lanes.tier` / `lanes.spoken` rows.

### Opt in to the Gemma fast lane

`install.sh` registers `com.zer0.z0live.gemma` when both prerequisites exist:

- Executable `~/.local/opt/llama.cpp/b12527/llama-b12527/llama-server` with Metal support.
- Readable `~/.local/share/z0live/models/gemma4-e4b.gguf` (Gemma 4 E4B Q4_0).

The LaunchAgent uses Metal for all layers, two slots, and an in-memory prompt cache. Verify startup with
`curl -fsS http://127.0.0.1:8650/health`. Expect `{"status":"ok"}`.

The `local` provider in `lane_server.py` reads `Z0LIVE_LOCAL_BASE_URL` at startup. It defaults to
`http://127.0.0.1:8650/v1`. Set a different loopback HTTP API base URL in the lane server's environment only
if needed. `first_s` measures the first visible streamed text; `/v1/ask` returns after generation completes.

After the coordinator deploys the new lane server, use [the proposed tiers file](examples/tiers-gemma.json)
to opt in. It races Gemma against three Haiku 5.5 calls and leaves medium and slow at their repo defaults.
Back up any existing `~/.config/z0live/tiers.json` before replacing it. Restore the previous file to roll
back, or remove the override if you were using the repo defaults. The installer does not edit this file.

To check the text lane without touching the live adapter, start a separate `lane_server.py` under the
Hermes runtime with `Z0LIVE_LANE_PORT=18646`. From `voice-adapter/`, run both checks against that port:

```sh
export Z0LIVE_LANE_URL=http://127.0.0.1:18646/v1/ask
.venv/bin/python slm_bench.py --hosted local:gemma4-e4b
.venv/bin/python bench/gemma_race.py
```

The race check compares the proposed fast tier to the same three Haiku lanes without Gemma. It sends only
fictional context and measures time to the first usable text reply, not time to speech.

## Two voices on one call

When the primary is GPT-Live, choose a guest in `~/.config/z0live/voices.json`. The adapter reads it at the start
of each call. Changing it does not restart or affect an open call.

```json
{"guest": {"engine": "gpt-live", "profile": "tek", "name": "tek", "voice": "ember"}}
```

Address Tek as `tek`, `teknium`, `டெக்`, or `டெக்னியம்`. Both GPT-Live sessions use the host's Codex ChatGPT
login but have separate WebRTC sessions and distinct voices. Tek does not get the primary's chat history or
helper lanes.

| Failure | Decision for two GPT-Live sessions |
| --- | --- |
| Tek loses the floor on a breath, pause, or `mm` | One call-local floor tracks the owner, reason, and start time. Provider turn boundaries and word timestamps drive the exchange. Mic onsets do not. An unnamed substantive turn after the reply returns to the primary. An explicit redirect can transfer the floor earlier. After 20 seconds idle, the floor returns to the primary unless Tek still has an answer in progress. Addressing Tek again restarts the 20 seconds. |
| A name is mentioned but not addressed | Accept a direct address at the start or within an utterance, such as `One thing, Tek, what shipped?`. `Tell Tek later` does not claim the floor. `Hermes, take this` or `back to Hermes` redirects it. A handoff phrase moves the floor only to a known voice name, so `Tek, back to you` and `back to the budget` do not, and it never overrides a direct address to Tek in the same turn. |
| Late guest or primary delegation | A voice delegates only while it holds the floor: a request addressed to Tek never runs in the primary's Hermes, and the reverse. Each answer waits only for its own leg's floor. On that floor it is sent at once, even while the voice is speaking, and a fast lane's guess still waiting is dropped when Hermes's answer for the same request arrives or the caller has asked something newer. An answer is delivered once that voice starts an audible reply; a barge-in does not replay it. If the voice stays silent for 15 seconds after it is free, or the floor moves first, the answer is sent once more, then dropped with `floor.answer_dropped`. The queue lives until the call closes. |
| Both voices answer or a muted reply leaks into the app | One output track selects one leg, never mixes PCM, and keeps a suppressed reply muted through its end. An audible reply keeps playing after the provider's `turn.done` until the output has been quiet for 0.3 s. The same decision filters assistant transcript events before they reach the app or call history. |
| Echo and barge-in | Keep the phone's acoustic echo cancellation. Do not mute the mic during playback. Reject recent matching playback text and short backchannels as interruption evidence. Confirmed caller speech cuts the selected leg's audio and transcript and clears buffered guest PCM. Detection is limited by the subscription provider's transcript delay; the in-process probe does not test room acoustics. |
| Guest transport dies | Keep the primary connected and queue one spoken disconnect notice. Transfer stranded guest answers to the primary with attribution. The guest Hermes host is still separate from the primary's chat. |
| The sessions lose conversational context | Send accepted spoken lines to the other leg as silent developer notes, up to 1,500 characters per reply and 12,000 per destination per call. |
| Two legs cost more CPU | Inspect and meter the mic once before fanning it out. Keep only the latest mic frame per leg. Skip guest resampling off-floor, use a view for peak detection, and reuse silence buffers. A Studio probe sample showed Opus encode/decode and resampling on the media path; it was not a production CPU comparison. |
| A floor change is hard to explain | Read `floor.changed` for the speaker and reason, `floor.barge_in` for interruptions, `floor.answer_queued` / `floor.answer_sent` / `floor.answer_dropped` (with a `reason`) for delivery, and `floor.delegation_dropped` for a request made off the floor. |

In the 11:35 call, Tek claimed `Hey Tek` at 84.736 s. A 35 ms mic burst revoked it before the 87.029 s guest
delegation. The primary speech at 105 s was the result of an older delegation. A silent leg can also keep one
provider turn ID open across multiple exchanges. The adapter separates new speech by provider word timestamps
so an old cumulative final cannot claim the floor again.

Run the guest's API-only Hermes host separately from the primary host on `:8644`. From the repo root:

```sh
Z0LIVE_HERMES_SOURCE="$HOME/.hermes/hermes-agent" \
Z0LIVE_HERMES_BIN="$HOME/.hermes/hermes-agent/.hermes/bin/hermes" \
python3 desktop/hermes_host.py --profile tek
```

This binds `127.0.0.1:18644` and `HERMES_HOME=~/.hermes/profiles/tek` without starting messaging or
schedulers. The host creates a private profile-specific API key. The guest uses a fresh Hermes voice session
per call. It never falls back to the primary's chat if its host is unavailable. If you change the host's
`--port`, match it with `Z0LIVE_GUEST_HOST_PORT` in the voice adapter.

`{"guest": "grok"}` still selects Grok (requires `hermes auth add xai-oauth`; no guest Hermes tools).
`{"guest": ""}` disables the guest. `Z0LIVE_VOICES_FILE` selects an alternate file for a probe without touching
live config. `Z0LIVE_GUEST=off` disables the guest for that adapter process.

To test without the phone, run `voice-adapter/context_probe.py` in-process with the guest host running and an
alternate voices file. Set `HERMES_VOICE_ENGINE=gpt-live`, `PROBE_GAP=15`, `PROBE_LISTEN=55`, and
`PROBE_ASK="Hey, can you hear me? || Tek, what's one thing you shipped this week? || Thanks. What's 12 times 12?"`.
Use `PROBE_PAUSE=0.55` and put `~` inside a turn to insert a 550 ms pause. Use `PROBE_SAVE` and
`PROBE_SAVE_IN` for local phone and caller WAVs. Check both signaling answers and `session.started` events.
Match `guest.delegation.done` against the Tek host session, then check `floor.answer_sent`, the transcript,
and the output audio. Primary and guest delegations both run by default. Set `PROBE_HERMES_FIXTURE=1`
to record primary requests without running them. Guest delegations still use the real Tek host.

## Primary hand-offs and call limits

The primary profile defaults to `poteto`. `Z0LIVE_PRIMARY_HOST_PROFILE` selects the profile for
both the API-only host and the voice adapter. The adapter uses the profile-qualified Responses API
on loopback port 8644. Calls for other profiles keep their legacy routing. Each primary call has its
own stored `z0live-primary-` conversation. The call brief seeds it. The adapter does not resubmit a
failed turn because Hermes may already have acted. Set `HERMES_VOICE_PRIMARY_DELEGATION=legacy`
to use the old chat or CLI path on new calls.

`HERMES_VOICE_MAX_CALL_SECONDS` defaults to 14400. Each GPT-Live leg uses its own advertised expiry.
The adapter prepares a silent replacement and swaps it in at a quiet boundary if time allows.
The new primary gets the original call brief and a bounded recap. Tek gets its guest persona and a
bounded call-only recap, never the primary's brief or chat history. Primary renewal does not touch
Tek's transport. Tek renews independently and keeps the same Tek Hermes session. The phone WebRTC
connection, floor, and answer queues stay in place across both swaps. If Tek's replacement fails and
the old guest disconnects, the primary gives one notice and relays any stranded guest answers.

`HERMES_VOICE_PROVIDER_CAP_SECONDS` can force a shorter provider deadline for a probe. At a 90-second
cap, allow at least 65 seconds before the follow-up ask. Look for `call.renewed`, `guest.renewed`
(if enabled), and spoken recall on the same call.

## Pick the voice model

```sh
bin/z0live engine            # show
bin/z0live engine grok       # Grok Voice (grok-voice-latest) via Hermes's xAI login
bin/z0live engine gpt-live   # GPT-Live over the ChatGPT subscription (default)
```

It sets `lanes.voice.actor` in the lanes config (validated write, hot-reloaded); the next call uses it.
Grok needs `hermes auth add xai-oauth --type oauth` once. `voice-adapter/xai_secret.py` runs under the
Hermes runtime and hands the adapter a client secret that expires in minutes; the login never leaves Hermes.
`HERMES_VOICE_ENGINE` in the adapter's environment overrides the config.

## Verify

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://100.100.77.46:9165/api/status          # 200 (dashboard via proxy)
curl -s http://100.100.77.46:9165/api/audio/voice-live/status                          # 401 without a Hermes login
cd voice-adapter && PATH=$PWD/.venv/bin:$PATH PROBE_ASK="What's the capital of Australia?" \
  env -u PYTHONPATH .venv/bin/python context_probe.py /dev/stdin <<< '[]'
```

The probe drives the real subscription voice with synthetic speech: `heard` must match the ask and
`catch_up_speech` must be empty. Real calls leave traces in `~/.local/share/z0live/voice-traces/<day>/`
(`trace_report.py` summarizes them).

## Known gaps (2026-11-19)

- Engines: GPT-Live and Grok Voice work. `on_device` (STT → Claude → TTS) is validated by the core but not
  built; a call configured for it runs on GPT-Live. Switching engines mid-call is not built.
- The phone app must use `:9165`. Taking over `:9119` means rebinding the dashboard to loopback.
- The semantic-router lanes (`Z0LIVE_SYSTEM_MODEL_URL`) point at the k3s cluster, which is being moved to a Studio control plane.

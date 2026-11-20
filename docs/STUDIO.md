# z0live on the Mac Studio

The Studio (`studio`, tailnet `100.100.77.46`) runs the whole voice stack natively under launchd. groot's
copy keeps working from its own tree; nothing here depends on groot.

## What runs

| LaunchAgent | Process | Listens | Job |
|---|---|---|---|
| `com.zer0.z0live.core` | `bin/z0live serve` | 127.0.0.1:8791 | Broker and lanes config: presets, spoken layouts, mid-call edits |
| `com.zer0.z0live.hermes` | `desktop/hermes_host.py` | 127.0.0.1:8644 | API-only Hermes host the core's lanes call |
| `com.zer0.z0live.voice` | `voice-adapter/mobile_live.py` | 127.0.0.1:9168 | Hermes app Live Voice over the ChatGPT subscription (codex login) |
| `com.zer0.z0live.proxy` | `voice-adapter/dashboard_proxy.py` | 100.100.77.46:9165 | Phone entry: voice-live paths to :9168, all else to the dashboard on :9119 |

All four start at login, restart on exit (`KeepAlive`, 10 s throttle) and log to `~/Library/Logs/z0live/<label>.log`.

Call path: Hermes app → `http://100.100.77.46:9165` → proxy → adapter → GPT-Live (`gpt-live-1-codex`) over
the codex ChatGPT login. Delegated work runs `hermes -p <app profile> chat` on the Studio. Lane turns go
adapter → core `/v1/lanes/turn` with the relay token.

## Install or repair

```sh
~/workspace/repos/z0live/deploy/macos/install.sh
```

Idempotent. It builds `voice-adapter/.venv` from `requirements.lock` on Hermes's bundled Python 3.14, writes
the four plists, pairs the relay token once and re-registers each job.

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
adapter (`:9168`, which writes `~/.local/share/z0live/voice-traces/<day>/*.jsonl`) and everything else to
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

## Two voices on one call

When the primary is GPT-Live, choose a guest in `~/.config/z0live/voices.json`. The adapter reads it at the start
of each call. Changing it does not restart or affect an open call.

```json
{"guest": {"engine": "gpt-live", "profile": "tek", "name": "tek", "voice": "ember"}}
```

Say “tek” or “teknium” in the turn you want tek to answer. An unnamed turn goes back to the primary. Both
GPT-Live sessions use the host's Codex ChatGPT login, with separate WebRTC sessions and distinct voices.
The phone plays one voice at a time. Each voice receives the other's spoken words as background, not a new
request. Tek does not get the primary's chat history or its helper lanes.

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
Use `PROBE_SAVE` and `PROBE_SAVE_IN` for the local phone and caller WAVs. Check both signaling answers and
`session.started` events, `guest.floor`, and `guest.delegation.done` with the same session ID in the tek host
log. `guest.failed` records guest setup errors; the primary continues alone. The probe's default delegation
fixture covers only the primary, so guest delegations in this test use the real guest host.

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

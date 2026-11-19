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

- Engine choice (`lanes.voice.actor`: `voice_plan` GPT-Live, `grok_voice`, `on_device` STT→model→TTS) is
  validated by the core but the adapter only drives GPT-Live so far.
- The phone app must use `:9165`. Taking over `:9119` means rebinding the dashboard to loopback.
- The semantic-router lanes (`Z0LIVE_SYSTEM_MODEL_URL`) point at the k3s cluster, which is being moved to a Studio control plane.

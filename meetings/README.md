# Hermes Meetings M1

A z0live plugin extension for a single private LiveKit room. `meetings_start`, `meetings_invite`, `meetings_status`, and `meetings_leave` manage a stock LiveKit server and a visible z0 participant. The bot uses the z0live phone adapter's `/api/audio/voice-live/session` WebRTC endpoint for the signed `kevin` microphone track and publishes the return audio to LiveKit. There are no changes to LiveKit or the z0live adapter.

## Development setup

- Use LiveKit 1.13.11 and a separate Python 3.13 environment with `meetings/requirements.txt`. The Hermes-side tool handlers use only the standard library. Reuse existing media dependencies or use a venv on a volume with space.
- Set `meetings_bot_python`, `meetings_voice_url` (a `127.0.0.1` HTTP origin), `meetings_voice_bearer_file` (a private file path), and optionally `meetings_profile` in the plugin configuration. Use a development adapter, not the live release. `meetings_start` with `bot: false` starts a room without the voice bridge.
- Loopback is the tested default. Start and leave are explicit and must run in the same Hermes plugin host; leave before a host reload or restart. Shutdown cleans up on normal Python exit, but not on a forced process kill; a new host never signals a PID from stale state. No services are installed. The optional macOS Tailscale bind is not qualified for client use. Do not enable public proxying or use this as an externally hosted service.
- A trusted, self-hosted upstream LiveKit Meet `/custom` UI accepts the `liveKitUrl` and `token` parameters in the guest link. `meetings_meet_url` defaults to `http://127.0.0.1:3000/custom`; the plugin does not serve that UI or configure TLS. Guest links are bearer credentials and are for private delivery only. `view` cannot publish; `full` can publish media and chat but cannot command the bot. An owner token is deliberately not issued by the chat tool or put in an invite URL; the local Python API can mint one for a trusted SDK client (see the self-test).
- Optional `meetings_guest_asr_url` accepts a loopback LFM2.5-Audio origin. The bot transcribes short guest mic segments by identity and keeps a bounded local transcript tail in the profile's meeting status file. Nothing from the guest goes to z0live, Hermes, or the room chat. If ASR is unset, guest speech is counted but not transcribed. No meeting audio is saved by the bot. The synthetic harness explicitly records its own fixture audio.

## Verification

```sh
python -B -m unittest meetings.test_meetings
FFMPEG=/path/to/ffmpeg /path/to/media-python meetings/selftest.py \
  --run /path/to/fresh/private/run \
  --adapter-dir /path/to/dev-z0live/voice-adapter \
  --adapter-python /path/to/adapter-python
```

The harness uses macOS `say`, a real dev z0live adapter, a disposable loopback mock for the dashboard auth check, and a local LFM2.5-Audio runner/model from the z0live model cache. It measures from the last non-silent frame of the question at the publisher to the first loud bot frame at a second participant. Results and fixture recordings go in the run directory. Use test audio only: the owner voice route uses the configured realtime voice provider. No browser/phone, multi-party overlap, lane attachment, notes, E2EE, or remote TLS acceptance is implied by this test.

# Desktop operations

## Host installation

Deployment targets Kevin's desktop `groot`, user `kvn`; the paths in `deploy/*.service` are deliberately
explicit. Inspect/adapt them for another machine. The broker needs only Python ≥3.10 stdlib. The native
Hermes API host uses `hermes --print-runtime-command` to select the installed interpreter and imports
`APIServerAdapter` from the installed source (`Z0LIVE_HERMES_SOURCE` can override it).
It does **not** instantiate GatewayRunner or start messaging/cron/kanban. Clean-profile credentials and
native session storage are reused; the unique `z0live` session is not another writer for an existing CLI.

```bash
cd /mnt/zer0models/workspace/zer0/products/z0live
chmod +x bin/z0live
install -d -m 700 /mnt/zer0models/workspace/zer0/data/z0live
# Refuse/inspect existing targets before replacing any links.
ln -s "$PWD/deploy/z0live.service" ~/.config/systemd/user/z0live.service
ln -s "$PWD/deploy/z0live-hermes.service" ~/.config/systemd/user/z0live-hermes.service
ln -s "$PWD/bin/z0live" ~/.local/bin/z0live
systemctl --user daemon-reload
systemctl --user enable --now z0live-hermes.service z0live.service
tailscale serve --bg --https=8791 http://127.0.0.1:8791
z0live pair s25 --qr
```

Install `qrencode` for QR output (Arch: `sudo pacman -S qrencode`). On the phone open
**z0live → Scan pairing QR → confirm**; when already paired the button is in Settings.
`z0live pair s25 --qr-png /private/new-pairing.png` writes a new **0600** PNG instead;
flags can be combined. Existing files and symlinks are refused. Delete the image after pairing.
QR modes never print a plaintext token/link, and pass the credential to qrencode through stdin,
not process arguments. If rendering fails the new device token is revoked. Without QR flags,
`z0live pair NAME` retains the printed token/deeplink fallback. Use it only in the intended
phone UI; do not paste it into chats or shell history. All QR/link tokens remain valid until
revoked (no short-lived activation exchange); protect terminal scrollback and QR screenshots.
Existing Tailscale routes remain untouched; this adds only port 8791. Never enable Funnel. User lingering
is already enabled on this host; services survive terminal logout and restart after crashes.

## Services/state

- `z0live.service`: `127.0.0.1:8791`, HTTP + one serialized bridge thread.
- `z0live-hermes.service`: `127.0.0.1:8644`, dedicated native API-only host.
- Hermes session `z0live`, title `z0live`, locked `anthropic/claude-opus-5-5`, repository cwd.
- Existing Garden Room services/API port 8643 are not modified or reused as a second cwd.
- Data: `/mnt/zer0models/workspace/zer0/data/z0live/` (outside the public repository).
  - `devices.json`: hashed device verifiers, names/revocation; 0600.
  - `.hermes-api-key`: local API key; 0600, never shared with phones.
  - `broker.db{,-wal,-shm}`: sessions, events, jobs, inbox, native session pointer.
  - `sessions/ID.jsonl`: event archive; recovered from canonical SQLite after partial writes.
  - `.service.lock`, `.export.lock`, `.devices.lock`: singleton/interprocess serialization.
  - optional `service.env`: non-secret broker settings, loaded by systemd.
- Hermes transcript/run state remains in the clean profile's native state.db; the broker never writes it.
- Journal: `journalctl --user -u z0live.service -u z0live-hermes.service -f`.

Settings: `Z0LIVE_DATA`, `Z0LIVE_BASE_URL`, `Z0LIVE_PORT`, `Z0LIVE_CODEX_AUTH`,
`Z0LIVE_SUMMARY_SECONDS` (300), `Z0LIVE_IDLE_SECONDS` (120), `Z0LIVE_CORS_ORIGINS` (exact comma-separated
additional web origins), `Z0LIVE_HERMES_API` (loopback 8644). The service's own HTTPS origin is allowed.
Voice model/voice are intentionally pinned to `gpt-realtime`/`marin` for this chosen route; no hidden
provider fallback. Do not put passwords/provider credentials into this environment file.

## Recovery

```bash
systemctl --user status z0live.service z0live-hermes.service
systemctl --user show z0live.service z0live-hermes.service -p MainPID -p ActiveState
tailscale serve status
```

Jobs persist exact native request JSON before HTTP and use stable `z0live-job-ID` Idempotency-Key.
After restart the bridge polls an existing run, rather than starting a new side-effecting turn. Terminal
failed/cancelled/interrupted runs are retained and reported, not replayed automatically. New runs follow
the native returned session ID if Hermes compresses. Reply insertion and completion checkpoint are atomic.
An unsuccessful setup seed blocks later jobs (`meta.seed_failed`): inspect the failed native run/provider,
then explicitly clear that flag and enqueue an appropriate recovery instruction; do not silently retry tools.
The broker's own retries have no authority to approve Hermes tools. Approval-required turns stay pending
and notify the phone to inspect the desktop. Keep serialized jobs intact while fixing upstream access.
Do not delete native sessions to fix OAuth. Refresh the existing Codex OAuth login if Realtime minting fails.

Events deduplicate on `(session_id,data.event_id)`; builtin/direct dispatch has queued acceptance semantics.
Do not blindly resend a direct/builtin work request after an ambiguous network timeout (inspect inbox first).
Device tokens have owner-level delegation power. Revoke a lost device and pair a fresh token; existing SSE
streams recheck revocation at their next 15-second heartbeat. Back up private data deliberately; do not
publish transcripts, tokens, native run dumps or the data directory.

To disable just this deployment:

```bash
systemctl --user disable --now z0live.service z0live-hermes.service
tailscale serve --https=8791 off
```

This leaves Garden Room, Telegram and other Tailscale routes running.

## Verification

Deterministic tests mock provider minting explicitly and cover auth/ownership, schema validation, JSONL
crash-tail repair, event dedupe/atomic rollback, active/end/idle summaries, builtin dispatch, inbox ordering,
native run idempotency, HTTP/SSE. No fake test output is treated as live evidence.

The opt-in `desktop/tests/live_webrtc.mjs` pairs a temporary test device, prints only the ephemeral secret
**length**, mints a real session, establishes a real headless Chromium/OpenAI text-mode WebRTC channel,
checks registered app tools, induces a real GPT `tell_hermes`, posts transcripts/tool receipts, explicitly ends,
then waits for both the native summary completion and a marker-bearing Hermes reply in inbox/SSE.
It sends no host microphone/audio. It uses HTTPS through the real Tailscale Serve listener, not a proxy mock.
The test leaves its private session history/device verifier as receipts; revoke test devices when done.
The test's SQLite receipt inspection assumes the standard local data path.

`python3 desktop/tests/live_recovery.py` additionally exercises direct send, feedback, supplied summaries,
client-local action receipts and service restarts; run only when restarting this deployment is safe.
`python3 desktop/tests/live_inbox.py` checks the exact populated/empty HTTPS inbox wrapper and device
revocation without invoking OpenAI/Hermes or a phone action; it revokes its temporary pairing itself.
Recorded live results and scope limits: [VERIFICATION.md](VERIFICATION.md).

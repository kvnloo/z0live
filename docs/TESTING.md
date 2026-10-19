# z0live real-phone rollout checklist

## 1. Consent, target and evidence

One agent owns the phone at a time. Ask before APK install, HOME/assistant changes, mic/audio or
screenshots (Phone Harness may send captures to the configured vision model). Owner unlocks and opens
the approved app; stop at lock/credential screens. No personal data, unrelated UI/logs, resets,
uninstalls, `pm clear`, forced roles or shared `adb kill-server`. Keep approved evidence outside Git
with `umask 077`, redact secrets, and agree retention.

Run `bin/phone-adb reconnect && bin/phone-adb status`; require **Verified saved phone**. Follow
[PHONE-SETUP.md](PHONE-SETUP.md) for offline/changed-port recovery and the fail-closed clean-profile
launcher (pinned Android target, telemetry off, per-call approval on).

## 2. Lawnchair APK from CI

- Workflow: **zer0 phone harness APK**, `kvnloo/lawnchair`, `.github/workflows/zer0-phone-harness.yml`.
  It runs contracts/formatting and `assembleLawnWithQuickstepPlayDebug`, then uploads
  `HyprSpaces-PhoneHarness-PlayDebug-<full SHA>` (30-day retention).
- Baseline checked 2026-10-19: [run 39143978836](https://github.com/kvnloo/lawnchair/actions/runs/39143978836),
  success, branch `experiment/hyprspaces-phone-harness`, SHA
  `ba091219e2d7c4cac8f173754d0c234f9ff866f0`; artifact was present/unexpired.
  **This is the old baseline, not proof of the new z0live integration.** Select a successful run
  whose exact source SHA includes the intended adapter. The workflow's push filter currently names
  the experiment branch, not `feat/z0live-realtime`; dispatch/update it only with release-owner approval.
- Inspect run/commit, download exact artifact, record APK SHA-256, `aapt dump badging` and
  `apksigner verify --print-certs`. Compare package/signature to the intended side-by-side build.
  Ask before `bin/phone-adb exec install -r /absolute/path/approved.apk`. On signature/version
  conflict, stop—obtain a correctly signed/newer build; never uninstall the owner's working launcher.
- Owner selects the test HOME only when ready. Verify home/app drawer, shortcuts, gestures and return
  to One UI Home. Pair z0live privately; test mic grant **and denial**, one harmless utterance, spoken
  reply, stop/hangup, app switching, network loss/reconnect, and screen-lock mic/service behavior.

## 3. Assistant role and Dash

- **Assistant:** inspect the intended package's eligibility and current role; owner uses
  Settings → Apps → Choose default apps → Digital assistant app (Samsung wording may vary).
  Do not force roles with adb. Verify explicit assistant invocation, truthful behavior if role denied,
  permission denial, visible ongoing-mic notification, clean stop, and restoration of the original assistant.
- **Dash:** [DASH.md](DASH.md) identifies the Expo app, source and build path. Open its demo/debug
  first for UI checks; then the approved live project/build. Test current bridge chat/Voice separately.
  A future z0live adapter must prove `client: "dash"`, token protection, real two-way audio, local tool
  result acknowledgement, inbox/SSE replay without double execution, and hangup/permission cleanup.
  Existing Dash voice or a debug screenshot does **not** prove z0live integration. Do not use an
  invented `dash://voice` link: production currently exposes pairing, not a Voice deep link.

## 4. Agent tools (approved test screen only)

```sh
# Capture command is adb -s VERIFIED_TARGET exec-out screencap -p through the pinned wrapper.
# Run only after screenshot consent, in an owner-approved private evidence directory:
umask 077
bin/phone-adb exec exec-out screencap -p > /private/evidence/approved-screen.png
bin/phone-adb exec logcat -d -s 'z0live:*' '*:S' > /private/evidence/z0live.log
# Only if hierarchy capture is authorized; filter to the test package locally before model ingestion:
bin/phone-adb exec shell uiautomator dump /sdcard/Download/z0live-test-ui.xml
bin/phone-adb exec exec-out cat /sdcard/Download/z0live-test-ui.xml > /private/evidence/ui.xml
bin/phone-adb exec shell rm /sdcard/Download/z0live-test-ui.xml
# After inspecting the current filtered hierarchy, use an approved resource/coordinate action:
# bin/phone-adb exec shell input tap X Y
```

Replace `/private/evidence` with a precreated restricted local directory, not the repository.
Check exit status before using any capture; a redirected file can be empty on failure. Tag-filtered
logcat may still contain sensitive app payloads: inspect/redact locally. `uiautomator` sees the whole
active window, so do not dump when private overlays/notifications are present. With Phone Harness,
load `phone-harness:phone-harness`, call `ensure_device()` first, then approved inspection/action helpers;
read back semantic state after each action. Do not route private screens through external decision tools.

## 5. Pass / block report

Run `nice -n 10 python -m unittest discover -s tests -p test_phone_adb.py -v` for deterministic wrapper
checks (mocks, not device proof). Report APK/source SHA, signer/package, phone transport and per-surface
pass/fail/blocked, real audio result, tool execution receipts, reconnect behavior and rollback. Backend
health/CI success alone is not end-to-end success; use [API.md](API.md) and [OPERATIONS.md](OPERATIONS.md)
for broker/real-WebRTC gates. No provider/phone test is run automatically by this checklist.

**This task:** wrapper tests and final live ADB reconnect/identity checks passed; no new pairing.
The bootstrapped Phone Harness CLI readiness probe passed, but integrated `phone_exec` failed with
`ModuleNotFoundError` despite doctor passing; see PHONE-SETUP. APK install, assistant role, Dash
UI/audio and z0live phone end-to-end tests are **not run**, not passed.

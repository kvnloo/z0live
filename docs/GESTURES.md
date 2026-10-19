# Oura gestures → z0live

Fork-only opt-in experiment. **No phone/ring or One UI acceptance has been run for this increment.** Offline tests are not evidence that a physical double tap starts audio.

## Paths, in priority order

1. **Standby in Lawnchair.** Pair z0live, grant microphone permission while the controls are visible, then press **Enable Standby**. The *same* `Z0LiveService` is promoted to `FOREGROUND_SERVICE_TYPE_MICROPHONE` before networking. Idle Standby constructs no WebRTC peer, microphone source or AudioRecord and requests no audio focus. It displays **z0live ready** with **Standby OFF**. Double tap begins a session inside that existing service; another double tap toggles mute; triple tap ends the session and returns to idle. OFF closes capture, disables command polling and removes the FGS. No boot start, sticky restart or wake lock. Android/OEM/process death requires visible rearming unless the separately selected assistant exemption applies. There is no prewarmed provider session: WebRTC mint/SDP latency remains.
2. **Selected default assistant.** Lawnchair declares `Z0LiveAssistantService`. The implementation checks BOTH `RoleManager.ROLE_ASSISTANT` and `VoiceInteractionService.isActiveService`, plus RECORD_AUDIO and the prior explicit gesture opt-in. The system-bound assistant can poll authenticated commands and request the microphone FGS through the documented VoiceInteractionService exemption. Declaring an assistant or checking a permission alone is not an exemption. Denied permission, foreground-start denial or OEM refusal is never reported as capture success. No hotword model or continuous audio.
3. **Desktop Tailscale ADB fallback.** Disabled unless the broker process has `Z0LIVE_GESTURE_ADB=1`. If the chosen phone target reports not-ready, the broker runs `adb devices` *at event time*, accepts exactly one authorized `100.*:port` transport, then invokes:

   ```sh
   adb -s PHONE_SERIAL shell am start -n app.lawnchair.play.debug/app.lawnchair.zer0.voice.z0live.Z0LiveActivity --ez autostart true
   ```

   Triple tap adds `--es gesture triple_tap`. No serial is compiled/configured, no connect/pair/install/root/shell string is used. Missing binary, no device, unauthorized device, multiple tailnet devices, launch errors and bounded timeout fail closed. There is no retry after an ambiguous POST. The debug Activity is exported **only in debug**, protected by `android.permission.DUMP` (shell/privileged, not ordinary apps). Release retains the nonexported controls. `autostart` is consumed only after the visible Activity resumes, and still requires pairing/mic permission. This is Activity dispatch, not an audio-start receipt; keyguard/background-start/Samsung restrictions can still require owner interaction.

### Android 14–16 / One UI evidence boundary

- [Android foreground-start / while-in-use restrictions](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start): Android 14+ checks microphone permissions at FGS creation; start while visible, or satisfy a listed exemption. The while-in-use list explicitly includes an app providing VoiceInteractionService. General FGS exemptions and microphone exemptions are distinct; battery exemption, a BLE event, ROLE_ASSISTANT alone and an ordinary background PendingIntent are not sufficient.
- [Microphone FGS type](https://developer.android.com/develop/background-work/services/fgs/service-types#microphone) and [Android 14 type requirements](https://developer.android.com/about/versions/14/changes/fgs-types-required): declare RECORD_AUDIO, FOREGROUND_SERVICE and FOREGROUND_SERVICE_MICROPHONE, promote with the microphone type while visible. Keeping that service continuously foreground avoids *creating* a new microphone service at gesture time. No microphone type is added later in background.
- [AOSP permission-lifetime change](https://android.googlesource.com/platform/frameworks/base/+/5d30b701587920280c12210af22f3457b802da6e%5E!/): stopForeground resets while-in-use admission. Standby therefore never demotes/re-promotes between sessions.
- [VoiceInteractionService](https://developer.android.com/reference/android/service/voice/VoiceInteractionService): the selected implementation is kept system-bound; verify selection, not merely manifest presence.
- The public contract documents background *continuation*, not a Samsung guarantee of delayed first AudioRecord initialization after hours idle. Standby's delayed capture is a standards-backed candidate, **not yet verified on Android 16 / S25 Ultra / One UI**. Verify idle has no privacy dot; capture has the mic indicator; triple-tap removes capture; repeat screen-off and after idle. Privacy indicator timing and Samsung process policy require the owner's phone. A running microphone-type FGS alone does not intentionally read audio. No guarantee of instant connection, battery cost or indefinite survival.

## Broker contract and authority

Every endpoint uses the existing paired device bearer (`Authorization: Bearer …`) over owner HTTPS/Tailscale, never URL credentials. Each sender has its **own** pairing, not Lawnchair's credential. Tokens remain encrypted/private and are never logged. No raw motion, BLE keys or audio are sent.

| Route | Request / response |
|---|---|
| `GET /v1/devices/self` | `{device_id}` authenticated opaque ID |
| `POST /v1/gesture/listener` | `{source_device,enabled,ttl_seconds}`; TTL 1–300; conflicting owner refused; disabling releases |
| `POST /v1/gesture` | `{gesture:"double_tap"\|"triple_tap",source_device,ts,nonce}`; Unix seconds ±1.5s, canonical UUID |
| `POST /v1/gesture/activity` | `{kind:"phone"\|"desktop",activity_ts,available,ready}`; device inferred from bearer; kind cannot change |
| `POST /v1/gesture/pin` | `{ttl_seconds:300}` pins **this** credential's target (“voice here”); 0 clears own pin |
| `GET /v1/gesture/commands` | `{commands:[{id,gesture,expires}]}` for this credential only; atomically claimed at most once |
| `POST /v1/gesture/ack` | `{id,status:"started"\|"muted"\|"ended"\|"denied"\|"failed"}` optional client receipt; never inferred from a claim |

Persisted nonce dedupe and command claims survive broker restart. Source must match bearer and own a live ring-listener lease. Commands expire in 3s; no stale replay. Exactly ONE ring listener at a time: NOOP while phone owns BLE, `.files` while PC owns BLE. The broker lease coordinates cooperative clients, **does not prove the other radio is OFF**. Stop and witness OFF before changing listeners. Lease/nonce persistence reserves ADB dispatch before the external effect; a crash in that gap is unknown/lost, never retried as a second toggle. Queued/claimed/Activity-dispatched statuses are NOT successful audio receipts.

### Context routing

Fresh heartbeat (15s) and available target required. Greatest **activity timestamp**, not latest polling time, wins; exact tie chooses phone. Pin overrides recency while that target is fresh/available, expires after at most 5min. Revoked device credentials are excluded. No target → no action. NOOP as source does not claim target authority. Phone signals unlocked screen transition plus z0live controls resume; a screen remaining unlocked does not synthesize repeated user activity. This is a coarse Lawnchair heartbeat, not all-app usage surveillance. Desktop compares Hyprland cursor position / active-window address without logging titles. Neither side reports private content. Keyboard-only activity within an unchanged desktop window and phone activity entirely inside another app are known signal gaps; use **Voice here** when necessary.

## Desktop voice target

```sh
npm ci --prefix desktop/tests
# Each token file must be mode 0600; supply via private environment, never a token argument.
Z0LIVE_BASE=https://YOUR_BROKER Z0LIVE_DEVICE_TOKEN_FILE=/private/desktop-token \
  node desktop/gesture-client.mjs
```

The opt-in visible Chromium client requests normal browser microphone consent on first start. Browser profile is private; bearer is process-memory only, never in URL/localStorage. It uses the **same `/v1/realtime/session`** and session-scoped tool dispatch as Android, hence PR #27's canonical L0 Realtime → L1 Haiku → L2 Opus → L3 workers, not Carly or another model stack. Double tap opens/starts or mutes; triple ends and releases tracks. Voice here button calls the pin endpoint. Startup/SDP/network failures stop local capture. This new desktop audio client needs acoustic/live-stack acceptance; JS parsing alone does not certify it.

## Senders

`.files` defaults to broker dispatch. Carly remains UI-only fallback behind **`--carly-fallback`**, never represented as mic capture. Replay stays dry-run (no network/actions). Example future owner session:

```sh
python scripts/oura-gestures.py listen --address "$RING_ADDRESS" --key-file "$PRIVATE_RING_KEY_FILE" \
  --broker https://YOUR_BROKER --broker-token-file "$PRIVATE_Z0LIVE_TOKEN_FILE" --seconds 120
```

NOOP setup accepts a separate owner pairing link in a password field, encrypts it with Android Keystore in noBackupFilesDir, and acquires the listener lease before enabling its existing bounded 120s ACM source. Three calibration double taps stay local; subsequent double/triple taps send only event envelopes. In broker mode it **never also fires** the old session capability. Stop/expiry/disconnect preserve explicit ring OFF; Stop releases the cooperative lease. Forget broker disables export. With no broker configured the prior visible-controls/capability path remains, and cannot cold-start hidden capture. Experimental broker copy is English fallback in localized resources pending translation.

## Owner session — NOT RUN, no ADB/install authorization implied

1. Owner backs up launcher/settings and chooses a test window. Download the **exact-head** APKs/metadata from the PR CI, independently compare SHA256 and signer. Do not use old `latest` assets as proof. No installs or ADB have been performed for this increment.
2. Owner updates APKs only if desired. Pair Lawnchair, allow microphone/notifications while visible. Enable Standby, dismiss controls: **z0live ready**, no audio and no mic indicator. OFF must remove notification and capture. Select Lawnchair as default assistant separately if testing that exemption.
3. Keep official ring app from simultaneously owning the ring; connect the already-owned Gen3 key in NOOP. **Never reset/rekey.** Pair NOOP's own broker credential, choose the phone target/pin if needed. Arm 120s, witness three calibration doubles, then a fourth double: session starts, mic dot appears, ask a short identity/tool question. Record only pass/fail and latency, not transcript/audio.
4. Fifth double mutes, next unmutes; triple ends and returns to ready/no capture. Repeat at home, another app, screen locked and after idle. Measure gesture classification → broker acceptance separately from connected/audio latency. Test desktop cursor activity vs phone activity/tie, Voice here, and switching listener only after OFF.
5. Negative controls: mic denied/revoked, broker down, Bluetooth off, ring disconnect, stale/replayed event, wrong token, conflicting listener, stale target, process kill and Standby OFF. No surprise local toggle or duplicate dispatch. ADB fallback is a **separate owner-opted future trial**; confirm authorized tailnet transport, keyguard behavior, dispatch vs acoustic latency; turn broker env flag OFF afterward.
6. Stop ring capture, observe OFF, release listener, Standby OFF, close desktop client. Confirm no mic dot, no audio session, no lingering motion stream. Record exact source/APK SHA, Android/One UI build, sanitized timings, false triggers and battery delta. Use PASS / FAIL / NOT RUN / BLOCKED. Do not publish serials, BLE addresses/keys, pairing links, raw movement or audio.

Offline qualification executes broker routing/auth/lease/nonce/command behavior and an HTTP→**injected fake ADB runner** latency test. It never executes real adb or accesses a phone. Fake-dispatch timings include loopback HTTP/SQLite/scheduling, not Tailscale, Activity resume, provider mint or acoustic latency. CI builds publish APK + SHA/commit/signer metadata. Hardware gates remain open.

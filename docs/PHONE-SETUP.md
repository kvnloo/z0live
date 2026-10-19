# Real-phone setup: S25 Ultra + Tailscale

## Status (2026-10-19, resumed check)

**Connected on final recheck (02:17 CDT):** after an initial offline interval, Tailscale ping succeeded in 8 ms; `phone-adb reconnect` connected to `100.117.226.39:46431`, verified the pinned hardware serial, `phone-adb status` reported **Verified saved phone**, and the model probe returned `SM-S938U`. Existing desktop trust worked without a new code. No phone UI, mic or APK test is claimed.

At 02:10–02:13 CDT the phone had been offline and the wrapper correctly failed closed. Connectivity is transient; always rerun status rather than treating this record or port as permanent.

**Earlier successful setup (2026-10-18, previous task trace):**

Phone: Samsung SM-S938U, hardware serial `R5CY93G2WNH`, tailnet IP `100.117.226.39` (`kevins-s25-ultra`). Desktop: `groot`, `100.113.138.100`. Setup probe: `tailscale ping --c 2 --timeout 5s kevins-s25-ultra` succeeded. Local Avahi discovered `_adb-tls-connect._tcp` on Wi-Fi `192.168.1.30:46431`; the **tailnet** connection `100.117.226.39:46431` succeeded using existing desktop trust, and `adb devices -l`, `get-state`, and `getprop ro.serialno` matched the expected phone. No new pairing code was needed. Port **46431 is an observation, not a stable port**. Port 5555 was closed; legacy TCP debugging was not enabled.

`bin/phone-adb` is also linked as `~/.local/bin/phone-adb`. It prefers `~/android/sdk/platform-tools/adb`, pins the tailnet IP and hardware serial, and saves only host/port/serial in `~/.local/state/z0live/phone-adb.json` (0600). It does not save pairing codes, scan ports, install apps, capture screens, or change phone settings on its own. Its explicit `exec` subcommand allows only `shell`, `exec-out`, `install`, `logcat`, and `get-state`; server-wide commands/target overrides are refused. These commands still need the appropriate owner approval; the wrapper is not a sandbox.

## Owner: what to do on the phone

No owner action or new pairing is needed while the final verified connection remains available. If offline, restore Tailscale/Wi-Fi, then try `phone-adb reconnect` and `phone-adb status`. Existing desktop trust may still work. If the port changed, follow step 7. For first setup or a lost/revoked pairing:

1. Keep Tailscale connected on phone and desktop. Unlock the phone yourself. Connect the phone to Wi-Fi. On Samsung, temporarily use the same home LAN as the desktop for pairing if VPN-only pairing is rejected.
2. Settings → About phone → Software information → tap **Build number** seven times if Developer options is absent. Enter the phone's PIN **on the phone**, never in chat.
3. Settings → Developer options → **Wireless debugging** → On; approve the selected trusted Wi-Fi if Android asks.
4. Tap **Pair device with pairing code**. Keep that dialog open. Read the **pairing port** and six-digit code from this dialog. This is not the main Wireless debugging page's connection port.
5. In a private desktop terminal run `phone-adb pair PAIRPORT`. The underlying `adb pair 100.117.226.39:PAIRPORT` prompts for the code; enter it there, never as a chat message, command-line argument, saved script, or agent log.
6. If Samsung rejects that route, run `phone-adb pair PAIRPORT --host WIFI_IP` using the Wi-Fi IP from the pairing dialog, on the same LAN. Once paired, return to Tailscale for the connection.
7. Close the pairing dialog. On the main **Wireless debugging** page, read **IP address & Port**. Run `phone-adb connect CONNECTPORT`. This uses `100.117.226.39:CONNECTPORT`, not the Wi-Fi IP. Confirm `phone-adb status` reports **Verified saved phone** and `device` (not `offline`/`unauthorized`).
8. Leave the phone unlocked on an approved test screen when authorizing a UI test. Agents must stop at the lock screen; no PIN/password/2FA entry or lock bypass.

If the code expires, open a new pairing dialog; never guess. If a connection is unauthorized, verify/forget/re-pair **only this desktop** in Wireless debugging's Paired devices list. Do not clear all ADB keys or kill the shared desktop ADB server.

## Reconnect and troubleshooting

```sh
phone-adb status
phone-adb reconnect         # saved tailnet endpoint, then same-LAN serial-specific Avahi discovery
phone-adb discover          # prints only this phone's LAN-advertised connect port, with tailnet IP
phone-adb connect NEWPORT   # current port from the phone screen; works without LAN mDNS
```

Wireless debugging can change its port after reboot, toggling debugging, or a network change. mDNS/Avahi is **LAN discovery**, not Tailscale discovery. Away from home, the owner must provide the current non-secret connection port; direct Tailscale connectivity must still be allowed by Android/Tailscale policy. Pairing survives port changes unless trust was revoked. Wi-Fi can be required by Android even with Tailscale available. Samsung Auto Blocker or managed-device policy may block ADB; let the owner decide whether to change security settings.

Optional legacy `adb -s 100.117.226.39:CONNECTPORT tcpip 5555` can give a fixed port on some devices, but it restarts adbd, may not persist across reboot, is not the Android 11+ TLS-pairing transport, and can expose authenticated ADB on **Wi-Fi/LAN too**, not just Tailscale. Do **not** enable it by default; require a separate owner decision, restrictive network access, verify the actual listener, and turn it back off after testing. Ordinary TLS wireless debugging is preferred.

## Phone Harness for Hermes profile `clean`

Installed via the reviewed catalog entry `phone-harness`, upstream `ShawnPana/phone-harness`, subdir `integrations/hermes`, pinned commit `7e3ea6cff1377db4d4f37e852c59493120aa52b6`. Manifest 0.1.0 depends on `phone-harness==0.3.2`. Location: `/workspace/hermes-home/profiles/clean/plugins/phone-harness`. `hermes plugins show phone-harness` reads back **enabled**; `hermes plugins doctor phone-harness` passes import/discovery/registration (one tool + one approval hook). The resumed runtime check `hermes --run-module phone_harness.run --version` returns 0.3.2. Both the previous run and the final resumed check executed a metadata-only `ensure_device()` through the bootstrapped module launcher against the explicit phone: `phone-harness device-ready`, exit 0. This did not capture a screen or drive UI.

**Integrated tool blocker:** a real `phone_exec` call in this resumed session failed before executing its snippet: `ModuleNotFoundError: No module named 'phone_harness'`. The installed plugin's `_command()` invokes `[sys.executable, "-m", "phone_harness.run"]`; that raw child interpreter lacks the dependency path supplied by Hermes bootstrap. Doctor/import success does not test this child execution. `hermes --run-module phone_harness.run` does resolve the installed dependency and its Android readiness probe succeeds. The plugin is **installed/enabled/discoverable, but integrated `phone_exec` execution is currently broken**. No vendor/core patch or owner restart was performed; use the verified pinned ADB helper for approved tests until the integration is repaired. The module launcher is a diagnostic/approved CLI alternative, not authority to bypass phone-action consent.

The tool is **`phone_exec`**: short Python snippets with Android helpers (`ensure_device`, `screen`, `screenshot`, `tap`, etc.). Android uses ADB screencap + uiautomator, not a companion app. Skill: `phone-harness:phone-harness` in a new session. It also supports iPhones/cloud phones; **do not use those backends for this task**. Three similarly named things are distinct: this Hermes plugin; the Lawnchair CI workflow **zer0 phone harness APK** (an Android launcher build, not the automation plugin); and the historical Tern `phone-controls` UI plugin on `omarchy-phone` (`100.104.42.85`).

For reproducible installation in **clean only**, the earlier run used the official plugin CLI (already done; no need to reinstall):

```sh
HERMES_HOME=/workspace/hermes-home/profiles/clean hermes plugins install phone-harness \
  --ref 7e3ea6cff1377db4d4f37e852c59493120aa52b6 --yes-deps --enable
HERMES_HOME=/workspace/hermes-home/profiles/clean hermes plugins doctor phone-harness --ci
```

This is a community plugin that executes Python with host-user permissions, not a sandbox. Keep the reviewed pin; do not update it implicitly.

No owner CLI session was restarted. The earlier installer reported hooks reloaded and tools available next session; **this resumed session now exposes `phone_exec`**. The device is now connected, but the child-interpreter blocker above remains. For a fresh, explicitly pinned local agent process (this recipe does not itself repair the plugin):

```sh
# Stop if reconnect or identity verification fails; never launch an unpinned fallback.
phone-adb reconnect &&
phone_env="$(phone-adb env)" &&
eval "$phone_env" &&
hermes --profile clean
# Exports: exact ANDROID_SERIAL, Android platform, SDK adb, telemetry=0, classifier auto-run=0
```

If already inside Hermes, start a new chat after applying these exports to its launcher environment; do not kill a running owner session. Keep per-call approval on: classifier auto-run is a heuristic, not a sandbox. Hermes plugin defaults telemetry off unless opted in; the exports explicitly force it off. Do not grant permanent/bot-wide approval. Never enable cloud rental or telemetry. Screenshots may be attached to the configured vision model, so only capture approved non-sensitive app screens. Cross-process agents must serialize phone ownership externally (the plugin lock is only in-process).

References: [Hermes plugin docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins), [plugin README at reviewed pin](https://github.com/ShawnPana/phone-harness/tree/7e3ea6cff1377db4d4f37e852c59493120aa52b6/integrations/hermes).

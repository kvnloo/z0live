# Dash: identification and z0live integration options

## What it is (source evidence)

**Dash is Kevin's Expo / React Native phone app, not a no-code dashboard or Kustom widget.**
Repository: https://github.com/kvnloo/dash. Local checkout:
`/mnt/zer0models/workspace/zer0/products/dash`, read at commit
`d5d191b0f7afc5c77b0902643da40b0c1a70c0be`. The previous task cloned this checkout;
this resumed task did not modify it.

| Source path (relative to Dash) | Evidence |
|---|---|
| `README.md` | Phone UI for desktop coding agents over Tailscale; per-harness chats, Bun bridge on port 4747; Expo Go and debug/demo startup recipes. |
| `app/package.json` | Expo `~57.0.21`, React Native `0.86.3`, TypeScript; `expo-audio`, `expo-secure-store`, `react-native-webview`; start/demo/debug scripts. |
| `app/app.json` | Name/slug Dash, scheme `dash`, Android package and iOS ID `app.dash.mobile`, microphone permission. |
| `app/src/screens/VoiceScreen.tsx` | Native Voice screen requests mic access, records via expo-audio, calls `sendVoiceBegin` / `sendVoiceChunk` / `sendVoiceCommit`, speaks streamed reply text. |
| `app/src/lib/voice-session.ts` | Local VAD and utterance-file chunks sent through the bridge; **not** a direct WebRTC Realtime session. |
| `app/App.tsx`, `app/src/navigation.ts` | Voice is an internal navigation route; production deep-link config currently maps Pair to `dash://connect`, not a Voice link. |
| `bridge/`, `shared/protocol.ts` | Bun WebSocket bridge/agent adapters and the shared wire protocol. |

## Build and test surface

From the Dash checkout, the README's developer path is:

```sh
cd bridge && bun install && bun run index.ts
# Separate terminal, from the Dash checkout:
cd app && bun install && bunx expo install && bunx expo start
```

On the phone, open the project in compatible Expo Go; pair with the desktop bridge using its
Tailscale address and private credential **only in the app's pairing UI**. Do not copy its token
into reports, screenshots, or an agent transcript. For isolated UI work without a live agent,
use `cd app && EXPO_PUBLIC_DEMO=1 bunx expo start` or `bun run debug`;
`bun run debug:shots` is a web screenshot harness, not proof of native mic/audio behavior.
These startup recipes are source-backed, not a claim that this task built/launched Dash.
A standalone/development Android build uses package `app.dash.mobile`; Expo Go uses its own
package and project URL instead, so do not assume the standalone package is installed.

## How z0live voice could be added (not implemented here)

1. **Native adapter (preferred full feature path):** keep Dash's Voice UI and add a separate
   z0live transport implementing [API.md](API.md). Pair a z0live device, store its bearer token
   in a protected store (expo-secure-store is already a dependency), and mint
   `/v1/realtime/session` with `client: "dash"`. Implement WebRTC audio/data-channel handling,
   local tool validation/execution, batched transcripts/events, inbox/SSE replies, and hangup.
   Keep the existing Dash bridge protocol separate rather than pretending it already speaks
   z0live. The current dependency list has no native WebRTC module: qualify a compatible module
   and development-client build first; do not promise this will run in stock Expo Go.
2. **Entry point / intent:** add an explicit user-tapped Voice button or a new production
   `dash://voice` route, then expose an Android `ACTION_VIEW` launcher/shortcut to that route.
   The route is a proposal, **not currently wired**. Also explicitly implement handling for the
   broker's `z0live://pair` link; Dash's existing `dash://connect` pairs to a different bridge.
   A deep link alone neither performs audio nor integrates the broker.
3. **WebView alternative:** build a real HTTPS z0live voice UI and open it from Dash's existing
   WebView dependency. The broker at port 8791 is an API, not an already-built voice web page.
   Prove mic permission, audio routing, WebRTC support and lifecycle on this Samsung before
   choosing this route. For a distinct web origin, allowlist that exact origin server-side;
   never embed a permanent token in public JS or use wildcard CORS.

Use [TESTING.md](TESTING.md) for the rollout gates and [PHONE-SETUP.md](PHONE-SETUP.md) for
real-phone transport. ADB / Phone Harness can drive either Expo Go or a native Dash build
once the owner opens the intended project; they do not depend on the UI's implementation
framework. No Dash adapter, APK installation, UI interaction or microphone capture was performed
by this resumed task.

# Passthru: z0 joins your other call

Say **"z0, join my meeting"** (or "merge the calls") on a z0 call. A browser on the Studio joins that
meeting as **z0 (Kevin's assistant)**, using the service's own, unmodified web app. Everyone in the meeting
hears z0, z0 hears them, and z0 can share a screen. There is no z0 server to run and no app to fork.

| Say | What happens |
|---|---|
| "z0, start a meeting" | z0 opens a new end-to-end encrypted **Element Call** room, joins it, puts the link in our chat |
| "z0, join my meeting" / "merge the calls" | z0 joins the link it finds: spoken, current calendar event, notification, screen, or chat |
| "share my phone" / "share the canvas" | z0 shares your phone screen (adb mirror) or the Atlas canvas through the app's own share button |
| "stop sharing", "z0, leave the meeting" | |
| "talk freely" / "just listen" / "only speak when someone says my name" | floor policy (default: addressed) |

Only Kevin's own phone leg can ask for **share** or **talk freely**. The same words spoken by someone in
the meeting are refused.

## Default service: Element Call

[call.element.io](https://call.element.io) is open source (element-hq/element-call) and E2EE by default. Guests
need no account; the room password sits in the link's `#fragment`, which browsers never send to the server.
Element plans to fold the standalone app into Element Web with guest links. When that ships, it is a new
recipe file. Switch services with `Z0LIVE_PASSTHRU_SERVICE` (any recipe with `create`).

## Extending: recipes

`voice-adapter/passthru/recipes/` has one small file per service: Element Call, Google Meet, Zoom (web),
Teams, Jitsi, and a generic fallback for custom apps, shared canvases and whiteboards. To support an app,
drop a file there or in a directory on `$Z0LIVE_PASSTHRU_RECIPES`. See the recipes README. Audio, camera and
screen share are shared by all recipes (`inject.js`), so a recipe is only the clicks. When an upstream app
moves a button, the fix is a few lines in its recipe.

## How it works

`meeting_leg.mjs` runs headless Chromium (Playwright). `inject.js` replaces the page's microphone with
z0's voice and taps every remote audio track; PCM moves over a loopback socket to the voice adapter, which
mixes the room into what z0 hears and tees z0's voice into the room (`passthru.py`, floor-gated). Screen
share is a CDP screencast of a page fed to the app's own `getDisplayMedia`.

The z0 browser profile is throwaway and runs with page CSP and Local Network Access checks off, so the
meeting page can reach the adapter on loopback. Traces redact every URL fragment (room keys).

## Verify

```sh
cd voice-adapter
.venv/bin/python -m unittest test_passthru
.venv/bin/python passthru/e2e_testroom.py              # local WebRTC page, both share paths
.venv/bin/python passthru/e2e_element.py               # real call.element.io: E2EE, audio both ways, share
```

## Limits

- Google Meet may hold z0 in "Ask to join" until someone admits it, and may block headless guests.
- On meet.jit.si, the room's creator must sign in (GitHub works), and meetings end after 95 minutes.
- The phone itself gives only one app the mic at a time. Passthru doesn't need it to, because z0 is a
  participant in the meeting.

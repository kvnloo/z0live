# Multi-caller calls (N231)

A second or third person can join a shared voice call over the tailnet. Callers hear the guest-profile
assistant through the adapter and can use direct WebRTC links to hear one another.

Open a new GPT-Live call with `X-Hermes-Profile: guest` (or `?profile=guest`) and enable
`Z0LIVE_MULTI_CALLER=on` on the adapter. Only the guest profile can open an invite-enabled room.
The room starts without chat history, and background requests use the isolated Hermes host provisioned
with `desktop/hermes_host.py --profile guest`. Use the guest host port (default 18644) and its own key.
If that host is unavailable, the room can still converse and reports that guest delegation is unavailable.

## How a call is joined today

- **Transport:** one WebRTC peer per call between the caller (the Hermes mobile app) and the adapter (`voice-adapter/mobile_live.py`, `Call.mobile`). Media is Opus over DTLS-SRTP. The adapter opens a second peer to GPT-Live (`SubscriptionLeg`) or a WebSocket to Grok (`grok_leg`).
- **Signalling:** a single HTTP exchange. The app POSTs its SDP offer to `/api/audio/voice-live/session` and gets the answer back (`Call.start`). The ICE configuration is empty (`iceServers=[]`), so only host candidates are used. That works because both ends are on the tailnet, and WireGuard carries the media. There is no STUN or TURN.
- **Auth:** `authenticate()` forwards the caller's bearer token to the Hermes dashboard's `/api/auth/me`. Only a dashboard identity (the owner) can open a call. `MAX_CALLS = 2` limits concurrent calls, and each of those calls has exactly one caller.
- **"People" today:** `call_brief.people()` and `Floor.people` exist (take 7, 11-22), but they handle several people sharing *one phone*. No second device can join.

## Design

```
 Ada (Studio browser) ──DTLS-SRTP──┐                      ┌── GPT-Live
                                   ├─ adapter: mix ▶ voice ┤
 Bob (Linux browser) ──DTLS-SRTP──┘   reply ◀ fan-out      └── (Hermes lanes)
        ▲
        └──────── direct DTLS-SRTP peer link (Ada⇄Bob): people audio never touches the adapter
```

1. **Invite (admission):** the host's authenticated client asks `/api/audio/voice-live/invite` for a capability. The request needs the dashboard bearer and the host's per-call member token. The capability is random (256 bits), works once, expires after 10 minutes, and dies with the call. Only its SHA-256 is stored. The invite is the only way in for a guest. A guest needs no Hermes account.
2. **Join:** the guest POSTs its SDP offer plus the invite to `/api/audio/voice-live/join`. The adapter gives the guest its own WebRTC peer (DTLS-SRTP, same as the host's) and returns a member token. The call allows at most 3 callers.
3. **Both voices reach the agent (uplink mix):** each guest's decoded frames queue up (at most 120 ms). The mixer then adds them into the host's microphone frames, on the host's frame clock, before `AudioBridge`. The voice provider, the level gate and the trace see one mixed stream. Its participants share the guest assistant. Mixing sums the samples and saturates at the int16 limits instead of wrapping. If a guest frame is missing, the host's frame passes through unchanged. If a frame has the wrong shape, it is skipped.
4. **Turn attribution:** the provider transcribes the mix, so it cannot tell who spoke. Per input turn, `Attribution` counts loud frames per caller and records overlap. The trace then gets `caller.turn {speaker, speakers, loud_frames, overlap_frames}` next to the transcript.
5. **The reply reaches everyone (downlink fan-out):** a `MediaRelay` over the host's output bridge sends the same agent audio to every caller. The output gate and floor logic run once, upstream, and are not repeated per caller. Each caller's copy is counted (`caller_out_frames:<id>`, `caller_out_loud:<id>`, plus a `caller.reply_audio` event).
6. **People to people, end to end:** the adapter does **not** mix the people for each other. Each pair of callers opens a direct browser-to-browser WebRTC link, a mesh with no SFU, fine for 2–3 people. The adapter relays only the SDP offers and answers, through a per-call mailbox at `/api/audio/voice-live/mesh/<call>` that requires a member token. DTLS-SRTP keys are negotiated between the two devices, so the adapter never holds them and never sees that audio.

All of this is behind `Z0LIVE_MULTI_CALLER=on`, and only for the GPT-Live engine. With the flag off, `Call.multi` is `None`, the new routes return 404, and the existing call path runs byte for byte as before.

### What "end to end" means here, honestly

- **Person ⇄ person:** end to end. The DTLS-SRTP keys exist only on the two devices, and the link runs over WireGuard on the tailnet.
- **Person ⇄ agent:** encrypted in transit (DTLS-SRTP, then WireGuard). The adapter decrypts the audio because the agent is a call member, as COLLAB.md requires. With GPT-Live as the voice, that audio goes on to OpenAI. A call that is private including the AI needs the local voice stack.
- **Remaining trust:** the signalling mailbox carries the DTLS fingerprints, so a malicious adapter could swap them and sit in the middle of a person-to-person link. Two ways to close that: show a short code derived from both fingerprints on each screen (SAS, as in ZRTP), or sign the offers with device keys pinned in the invite. Neither is in this slice.

## What ships in this slice

| Piece | Where |
|---|---|
| Flag, invite, join, mailbox routes | `voice-adapter/multi_caller.py` (`routes`, `Invites`, `Mailbox`) |
| Uplink mix + turn attribution | `multi_caller.mix`, `Attribution`, `Room.mix_frame` |
| Reply fan-out | `Room.fan_out` (hooked in `Call.attach_primary_output`) |
| Hooks in the call | `mobile_live.py`: `Call.multi`, the mic wrap, the output wrap, `end_turn`, close, `multi` in the session answer |
| Unit tests | `voice-adapter/test_multi_caller.py` (mix, saturation, attribution, invites, mailbox, room mix) |

## Missing (next slices, in order)

1. **A join page and a link in the app.** The Hermes mobile app needs an "Invite" button that mints the link (invite in the URL fragment, never in the query string), and the adapter must serve a guest page through the dashboard proxy on the tailnet.
2. **Names into the agent's context.** Attribution only reaches the trace today. Next, add a short context note per turn ("Bob said: …") so the agent can address people by name.
3. **Per-caller mute and leave in the UI.** Host mute still mutes the whole mix, guests included.
4. **Fingerprint verification (SAS)** for the person-to-person links.
5. **Guests outside the tailnet**, with SFrame + MLS per COLLAB.md. That needs the owner's decision on a public join point (Funnel or TURN), which is still open from the 11-20 answer.
6. **Grok engine:** its leg has no shared output track yet, so multi-caller is off for Grok.

## Verification

The automated checks cover single-use invites, signalling, the real Opus frame layout, and the
guest-profile boundary. Speaker attribution is intended for display. It carries no authority.

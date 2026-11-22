# End-to-end encrypted collab calls

Kevin hosts a call that friends can join by link. The relay forwards encrypted media and events. The AI appears in the participant list and holds keys like any other endpoint. The relay never gets those keys.

The [Oh My Pi collab flow](https://omp.sh/docs/collab) is the model for bearer invites, full and view-only permissions, and `/collab stop`. The [loopback prototype](../collab/README.md) implements the event path only. It has no audio, AI, room administration, or production compatibility claims.

## Threat model

- A relay that only forwards traffic cannot read audio, messages, reactions, transcripts, or control payloads. It can see room IDs, connections, IP addresses, routing metadata, frame lengths, and timing. It can drop, delay, replay, or reorder traffic. Encryption does not make the relay available or hide who is talking.
- A key holder can hear the call and copy what they receive. Neither view-only access nor a key rotation erases data a member already received. A leaked invite grants its holder the invite's permissions until admission is disabled or the invite expires.
- A fragment is not part of the HTTP request. It is still visible to the invite recipient, browser scripts, and anyone with access to the copied link or clipboard. Do not put keys in query strings, signaling, logs, crash reports, analytics, or durable browser storage.
- The demo serves the page and relay from the same loopback process. Trust that local executable and browser. A relay that can substitute client JavaScript can read the fragment. For real rooms, ship the client through a separate trusted origin or signed app. Trusting HTTPS on the relay does not fix malicious client delivery.
- A compromised participant or Studio AI process sees plaintext. The AI is not a blind service. No hosted model is inside the proposed collab trust boundary. Use local speech recognition, inference, and speech synthesis, or leave the AI out. Never send Telegram, chat history, or other private context to hosted models.

## Key distribution and roles

For the demo, the browser generates an independent public room ID and a 256-bit room key with Web Crypto. The path carries the room ID. The fragment carries the room key. Both browsers import the same AES-GCM key. Each event gets a fresh random 96-bit nonce and a 128-bit tag. Associated data binds the packet to the room and the event protocol version. The relay forwards the hex-encoded nonce and tagged ciphertext unchanged. No transport field contains the event type or text. The demo has no sender authentication, replay filter, membership change, or forward secrecy. Use synthetic content and discard its links after the test.

For real rooms, treat the invite as a short-lived admission capability, not a reusable live media key. Give each invite a random secret in the fragment and pin the host's public identity in the trusted invite. On redeem, bind proof of the invite to the joining device's public key and a fresh challenge. The host validates the capability and authorizes a device and role. Deliver the current epoch's keys over an authenticated encrypted channel to that device. Use a reviewed group key protocol such as [MLS](https://www.rfc-editor.org/rfc/rfc9420) for authenticated membership and epoch changes. Do not extend the demo's shared key into a group key protocol.

| Role | Allowed behavior |
| --- | --- |
| Host | Invite, admit, remove, and change roles. Add or remove the AI. Set room policy and run `/collab stop`. |
| Full guest | Receive media and events. Speak, send events, and address the AI within room policy. No owner tools or host controls. |
| View-only guest | Receive live media and events. No publishing or AI commands. No private history. |
| AI | Receive content as a visible room member and publish its own voice and events. No host controls. |

Full and view-only links convey distinct admission scopes. A view-only device must decrypt media, so it can compute symmetric authentication tags with its receive keys. Do not implement view-only by hiding buttons or by trusting a role inside an AES-GCM payload. Bind sender identity and role to a host-signed roster. Authenticate application events with device signing keys and verify the roster and event privileges at every receiver. For media, enforce publish roles with authenticated relay sessions and bind SFrame senders to authorized streams. If a malicious relay and a viewer can collude, shared SFrame decryption keys alone cannot prove sender identity. Require sender authentication if that adversary is in scope.

Rotate media and event keys on joins and removals. Do not give new members old keys or a transcript by default. Distribute new keys only to the new roster and reject stale epochs after the cutover. Use per-sender counters and replay windows. Derive separate keys for event and media traffic. The host's signed `/collab stop` ends the session, closes media and event streams, clears active keys, and invalidates invites. The host also requests relay teardown. If the relay suppresses stop, expiring membership leases bound the time a disconnected client keeps accepting the old session. Restart with new room IDs and secrets.

## Media encryption

Use [SFrame](https://www.rfc-editor.org/rfc/rfc9605) or a reviewed compatible implementation on the encoded WebRTC media path. An [encoded transform](https://www.w3.org/TR/webrtc-encoded-transform/) encrypts each compressed Opus frame after encoding and before RTP packetization. A receiver decrypts after depacketization and before decoding. A Selective Forwarding Unit (SFU) forwards the encrypted payload without the media keys. The SFU can terminate hop-level DTLS-SRTP and use the RTP headers it needs to route. DTLS-SRTP alone is not E2EE through a terminating SFU.

Install the send and receive transforms before enabling media. Use per-sender keys and a unique counter per frame under the active key, with a new key if the counter resets. Protect each full encoded frame, not individual RTP packets. Agree on the cipher suite and key IDs, retain only the keys needed for the defined cutover, and handle loss and reordering within bounded replay windows. Authenticate signaling and reject failed tags, unknown senders, and unexpected epochs. Unsupported endpoint, missing transform, or missing key means no audio, not fallback to plaintext.

Use app-level AES-GCM for chat, reactions, and controls. Encrypt before passing events to HTTP, WebSocket, or a data channel. Audio could also use app-level encrypted Opus chunks, but the app would need its own packetization, jitter handling, playback timing, and congestion behavior. Prefer encoded transforms so WebRTC still handles those jobs. The demo's event protocol is not SFrame and must not be reused for audio.

## What Studio must change

Today, `voice-adapter/mobile_live.py` creates separate phone and provider WebRTC peers. `Call` receives decoded phone audio via `AudioBridge`, passes it to GPT-Live or Grok, and translates provider events for the phone. The adapter can read and trace plaintext. Adding room encryption to that path would not make the provider leg private.

1. Add a separate collab session path without changing the existing solo call. Keep forwarding and admission separate from the AI endpoint. The relay or SFU never gets room keys or decoded audio. Use a distinct loopback process or tailnet HTTPS with explicit ACLs. Do not use a public listener or Tailscale Funnel.
2. Make a Studio AI process join with a device identity like any other participant. Show it in the host-signed roster before it can decrypt. Run the media transform in a supported browser or native endpoint, then pass decoded audio to the local voice pipeline. Qualify the Python WebRTC stack before assuming that the current `aiortc` track API exposes the encoded frame boundary. Encrypt AI output as the AI's own stream. Never mix or transcode participant audio in the blind relay.
3. Keep the collab AI's input and tools scoped to the room. Do not call the current provider bridge, context append, chat continuity, hosted lanes, or owner tools. Disable plaintext recording, transcript persistence, and call trace bodies by default. If any local tools are allowed, require a collab-specific allowlist and host approval. An AI member leaving must close its streams, cancel pending work, and release keys.
4. Verify browser and phone transform support with synthetic Opus before adding real calls. Prove two endpoints can hear each other while the SFU cannot decode a capture. Exercise wrong keys, tampering, replay, role violations, removal, and stop. Test that collab cannot fall back to provider or plaintext paths, including when the local AI fails. Measure audio delay and loss recovery before choosing an SFU or widening access.

# Try the collab prototype

Use Node.js 22 or newer and synthetic messages. The relay binds only to `127.0.0.1`. Do not publish or proxy the demo.

1. Run `node collab/relay.mjs` from the repository root. No dependencies are needed to serve the demo.
2. Open `http://127.0.0.1:8788` and select **Create room**.
3. Copy the complete **Invite link**, including the fragment, into a second browser or a private window on the same machine.
4. Wait for **Connected** in both windows. Send a message in one and select **Wave** in the other. Both windows show both events. A late joiner does not see old events.
5. Close both windows and stop the relay with Ctrl+C. The demo has no host control or invite revocation. Do not reuse the link.

The page removes the fragment from the address bar after reading it. To rejoin after a reload, use the original invite link. You can pass a different loopback port as the first argument to `relay.mjs`.

## Run the browser test

```sh
cd collab
npm ci
npx playwright-core install chromium
npm test
```

If Chromium is already installed elsewhere, skip the install command and set `CHROMIUM_PATH` to its executable. The test runs separate Chromium sessions against an ephemeral loopback port. It inspects the relay's incoming HTTP requests and subscribes without a key to observe outgoing bytes. The two browsers render the synthetic events. The relay sees the same opaque envelopes in each direction, with neither the fragment secret nor the text in its requests. The test decrypts the captured envelopes independently with the correct key and verifies that a wrong key fails and the browser rejects tampering.

This test checks the demo's actual wire path. It does not defend against a relay that replaces the served client code or a copied invite. See the [collab design](../docs/COLLAB.md) for the trust boundary and the remaining work.

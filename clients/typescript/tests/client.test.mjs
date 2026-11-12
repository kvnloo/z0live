import assert from "node:assert/strict";
import test from "node:test";
import {
  BrokerError, DesktopBrokerClient, memoryToken,
} from "../dist/index.js";

const ORIGIN = "https://example.ts.net:8791";
const sid = "synthetic-session-1";
const goodMessage = (id, text = "synthetic") => ({
  id, say: text, action: null, params: {}, session_id: sid,
});
function createClient(transport, overrides = {}) {
  const bearer = memoryToken("synthetic-token");
  const store = { after: 0,
    load() { return this.after; }, save(v) { this.after = v; } };
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    return transport(url, init);
  };
  const client = new DesktopBrokerClient({
    origin: ORIGIN, token: bearer.provider, fetch,
    nowSeconds: () => 1000, cursorStore: store, timeoutMs: 1000, ...overrides,
  });
  return { client, calls, store, bearer };
}
const json = (object, status = 200) =>
  new Response(JSON.stringify(object), {
    status, headers: { "Content-Type": "application/json" },
  });
const page = (...messages) => ({
  messages, after: messages.at(-1)?.id ?? 0, has_more: false,
});
async function errorKind(promise, kind) {
  await assert.rejects(promise, err => {
    assert.equal(err instanceof BrokerError, true);
    assert.equal(err.kind, kind);
    return true;
  });
}

test("HTTPS origin enforced; token stays injected header, not URL, storage or error", async () => {
  for (const origin of [
    "http://example.ts.net", "https://user:secret@example.ts.net",
    "https://example.ts.net/path", "https://example.ts.net?token=x",
  ]) {
    assert.throws(() => createClient(() => json({}), { origin }));
  }
  const { client, calls, store } = createClient(() => json(page()));
  assert.deepEqual(await client.getInbox(), page());
  assert.equal(store.after, 0, "reads do not acknowledge");
  assert.equal(calls[0].url, ORIGIN + "/v1/inbox?after=0");
  assert.equal(calls[0].init.headers.Authorization, "Bearer synthetic-token");
  assert.equal(calls[0].init.redirect, "manual");
  assert.equal(calls[0].init.credentials, "omit");
  assert.equal(calls[0].init.cache, "no-store");
  assert.doesNotMatch(calls[0].url, /synthetic-token/);
});

test("hostile inbox schema, duplicate IDs and malformed JSON fail closed", async () => {
  const invalid = [
    { messages: "array", after: 0, has_more: false },
    { messages: [goodMessage(1)], after: "1", has_more: false },
    { messages: [goodMessage(1), goodMessage(1)], after: 1, has_more: false },
    { messages: [goodMessage(2)], after: 1, has_more: false },
    { messages: [{ ...goodMessage(1), action: 3 }], after: 1, has_more: false },
    { messages: [{ ...goodMessage(1), params: null }], after: 1, has_more: false },
    { messages: [{ ...goodMessage(1), session_id: "bad/../sid" }], after: 1, has_more: false },
    { messages: [], after: 0, has_more: "false" },
  ];
  for (const response of invalid) {
    const { client } = createClient(() => json(response));
    await errorKind(client.getInbox(), "protocol");
  }
  const { client } = createClient(
    () => new Response('{"unfinished":', { headers: { "Content-Type": "application/json" } }));
  await errorKind(client.getInbox(), "protocol");
});

test("mutation never retries, GET may retry bounded 5xx, revoked token stays auth", async () => {
  let attempts = 0;
  const retry = createClient(() => {
    attempts++;
    return json(attempts < 3 ? { error: "synthetic outage" } : page(),
      attempts < 3 ? 503 : 200);
  });
  assert.deepEqual(await retry.client.getInbox(0, { retries: 2 }), page());
  assert.equal(attempts, 3);
  const blocked = createClient(() => {
    attempts++;
    return json({ error: "revoked" }, 401);
  });
  await errorKind(blocked.client.getInbox(0, { retries: 2 }), "auth");
  assert.equal(blocked.calls.length, 1);
  const mutation = createClient(() => {
    attempts++;
    return json({ error: "synthetic outage" }, 503);
  });
  await errorKind(mutation.client.callBuiltin(
    "tell_hermes", sid, { kind: "request", text: "synthetic" },
  ), "http");
  assert.equal(mutation.calls.length, 1, "uncertain mutation not silently reissued");
});

test("mint is typed ephemeral and failed validation does not call transport", async () => {
  const response = {
    value: "synthetic-ephemeral", expires_at: 1100,
    model: "test-realtime", voice: "test-voice",
    instructions_version: "synthetic-v2", session_id: sid,
  };
  const { client, calls } = createClient(() => json(response));
  assert.deepEqual(await client.mint({ client: "web", context: {} }), response);
  assert.equal(calls[0].url, ORIGIN + "/v1/realtime/session");
  assert.equal(JSON.parse(calls[0].init.body).client, "web");
  await errorKind(client.mint({ client: "web", tools: [{
    type: "function", name: "tell_hermes", parameters: { type: "object" },
  }] }), "protocol");
  assert.equal(calls.length, 1, "invalid registration does not mint");
  const expired = createClient(() => json({ ...response, expires_at: 500 }));
  await errorKind(expired.client.mint({ client: "web" }), "protocol");
});

test("typed queued tools, event batches, sessions and cleanup", async () => {
  const responses = new Map([
    ["/v1/tools/tell_hermes", { ok: true, id: 3, status: "queued" }],
    ["/v1/tools/get_context", { session_id: sid, client: "web", context: { app: "synthetic.app" } }],
    ["/v1/tools/summarize", { ok: true, status: "queued" }],
    ["/v1/events", { ok: true, accepted: 1 }],
    ["/v1/sessions", { sessions: [{
      id: sid, client: "web", created: 1000.1, updated: 1001.2, ended: 0,
    }] }],
    ["/v1/context?session_id=" + sid, { session_id: sid, client: "web", context: {} }],
    ["/v1/sessions/" + sid + "/end", { ok: true, id: null }],
  ]);
  const { client, calls } = createClient(url => {
    const path = url.slice(ORIGIN.length);
    assert.ok(responses.has(path), path);
    return json(responses.get(path));
  });
  assert.equal((await client.callBuiltin("tell_hermes", sid,
    { text: "unit", kind: "request" })).status, "queued");
  assert.equal((await client.callBuiltin("get_context", sid)).client, "web");
  assert.equal((await client.callBuiltin("summarize", sid, { text: "synthetic handoff" })).status,
    "queued");
  assert.equal(await client.postEvents([{
    session_id: sid, type: "tool_call", text: "", ts: 1000.75,
    data: { event_id: "synthetic-1", name: "phone_local" },
  }]), 1);
  assert.equal(JSON.parse(calls[3].init.body)[0].ts, 1000.75);
  assert.equal((await client.listSessions())[0].ended, 0);
  assert.deepEqual(await client.getContext(sid), {
    session_id: sid, client: "web", context: {},
  });
  assert.deepEqual(await client.endSession(sid), { ok: true, id: null });
  const count = calls.length;
  await errorKind(client.postEvents([{
    session_id: sid, type: "wrong", data: [],
  }]), "protocol");
  assert.equal(calls.length, count, "event rejects before dispatch");
});

test("cursor only advances after explicit ack; repeats are not reexecuted", async () => {
  const { client, store } = createClient(() => json(page(goodMessage(1), goodMessage(2))));
  const result = await client.getInbox(0);
  assert.equal(store.after, 0);
  assert.equal(await client.acknowledge(result.messages[0]), true);
  assert.equal(store.after, 1);
  assert.equal(await client.acknowledge(result.messages[0]), false);
  assert.equal(store.after, 1);
  assert.equal(await client.acknowledge(result.messages[1]), true);
  assert.equal(store.after, 2);
});

function splitUtf8Sse(text, offset) {
  const bytes = new TextEncoder().encode(text);
  return [bytes.slice(0, offset), bytes.slice(offset, offset + 1),
    bytes.slice(offset + 1)];
}
function sseResponse(parts) {
  const stream = new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });
  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream" },
  });
}

test("SSE split UTF-8 / fields / heartbeat and inbox message parity", async () => {
  const original = goodMessage(42, "café ☕ synthetic");
  const packet = ": z0live connected\n\nid: 42\nevent: message\ndata: " +
    JSON.stringify(original) + "\n\n: heartbeat\n\n";
  const bytes = new TextEncoder().encode(packet);
  const multi = bytes.indexOf(0xc3); // first byte of é in UTF-8
  assert.ok(multi > 0);
  const parts = splitUtf8Sse(packet, multi);
  const { client } = createClient(() => sseResponse(parts));
  const found = [];
  for await (const event of client.stream(41)) found.push(event);
  assert.deepEqual(found, [{ event: "message", id: 42, data: original }]);
});

test("SSE mismatch rejected, cancellation stops stream and abort is forwarded", async () => {
  const wrong = { ...goodMessage(5), id: 4 };
  const bad = createClient(() => sseResponse([new TextEncoder().encode(
    "id: 5\nevent: message\ndata: " + JSON.stringify(wrong) + "\n\n",
  )]));
  await errorKind((async () => {
    for await (const _ of bad.client.stream(0)) { /* no-op */ }
  })(), "protocol");

  const first = goodMessage(8);
  const { client } = createClient(() => sseResponse([new TextEncoder().encode(
    "id: 8\nevent: message\ndata: " + JSON.stringify(first) + "\n\n",
  )]));
  const seen = [];
  for await (const evt of client.stream(0)) {
    seen.push(evt.id);
    break; // generator must cancel reader in finally
  }
  assert.deepEqual(seen, [8]);

  const controller = new AbortController();
  controller.abort();
  const aborted = createClient((_, init) => {
    if (init.signal.aborted) {
      const err = new Error("aborted");
      err.name = "AbortError";
      throw err;
    }
    return json(page());
  });
  await errorKind(aborted.client.getInbox(0, { signal: controller.signal }), "aborted");
});

test("server redirects refused and invalid queued/completed claims rejected", async () => {
  const redirected = createClient(() => new Response(null, {
    status: 302, headers: { Location: "https://hostile.invalid" },
  }));
  await errorKind(redirected.client.getInbox(), "http");
  const falseClaim = createClient(
    () => json({ ok: true, id: 3, status: "completed" }));
  await errorKind(falseClaim.client.callBuiltin(
    "tell_hermes", sid, { kind: "request", text: "synthetic" }), "protocol");
});

test("non-finite retry limits fail before sending a request", async () => {
  for (const retries of [NaN, Infinity, -Infinity]) {
    // Succeed on the fourth call so a broken unbounded retry loop terminates.
    let attempts = 0;
    const { client, calls } = createClient(() => {
      attempts++;
      return attempts <= 3 ? json({}, 503) : json(page());
    });
    await errorKind(client.getInbox(0, { retries }), "configuration");
    assert.equal(calls.length, 0);
  }
});

test("JSON body timeouts retain timeout classification after headers arrive", async () => {
  const { client, calls } = createClient((_, init) => new Response(
    new ReadableStream({
      start(controller) {
        init.signal.addEventListener("abort", () => {
          controller.error(new DOMException("aborted", "AbortError"));
        }, { once: true });
      },
    }),
    { headers: { "Content-Type": "application/json" } },
  ), { timeoutMs: 5 });
  await errorKind(client.getInbox(), "timeout");
  assert.equal(calls.length, 1);
});

test("SSE body timeouts retain timeout classification after headers arrive", async () => {
  const { client } = createClient((_, init) => new Response(
    new ReadableStream({
      start(controller) {
        init.signal.addEventListener("abort", () => {
          controller.error(new DOMException("aborted", "AbortError"));
        }, { once: true });
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  ), { timeoutMs: 5 });
  await errorKind(client.stream().next(), "timeout");
});

test("JSON and SSE body cancellation remains distinct from network failure", async () => {
  for (const streaming of [false, true]) {
    for (const cancel of [false, true]) {
      const caller = new AbortController();
      const { client } = createClient((_, init) => new Response(
        new ReadableStream({
          start(controller) {
            init.signal.addEventListener("abort", () => {
              controller.error(new DOMException("aborted", "AbortError"));
            }, { once: true });
            queueMicrotask(() => {
              if (cancel) caller.abort();
              else controller.error(new Error("transport body failed"));
            });
          },
        }),
        { headers: { "Content-Type": streaming ? "text/event-stream" : "application/json" } },
      ));
      const result = streaming ? client.stream(0, caller.signal).next()
        : client.getInbox(0, { signal: caller.signal });
      await errorKind(result, cancel ? "aborted" : "network");
    }
  }
});


test("SSE frame limits are independent of transport chunk batching", async () => {
  const frames = Array.from({ length: 1500 }, (_, i) =>
    `id: ${i + 1}\ndata: ${JSON.stringify(goodMessage(i + 1))}\n\n`);
  const packet = frames.join("");
  assert.ok(packet.length > 131072);
  for (const parts of [[packet], frames]) {
    const { client } = createClient(() => sseResponse(
      parts.map(part => new TextEncoder().encode(part))));
    const ids = [];
    for await (const event of client.stream()) ids.push(event.id);
    assert.deepEqual(ids, frames.map((_, i) => i + 1));
  }
});

test("SSE oversized complete and incomplete frames remain rejected", async () => {
  for (const ending of ["", "\n\n"]) {
    const oversized = ":" + "x".repeat(131072) + ending;
    for (const parts of [[oversized], [oversized.slice(0, 65536), oversized.slice(65536)]]) {
      const { client } = createClient(() => sseResponse(
        parts.map(part => new TextEncoder().encode(part))));
      await errorKind(client.stream().next(), "protocol");
    }
  }
});

test("SSE supports CR, LF and CRLF lines across transport boundaries", async () => {
  const original = goodMessage(1, "synthetic café");
  for (const newline of ["\r", "\n", "\r\n"]) {
    const packet = [": heartbeat", "", "id: 1", "event: message",
      "data: " + JSON.stringify(original), "", ""].join(newline);
    const bytes = new TextEncoder().encode(packet);
    for (const parts of [[bytes], Array.from(bytes, byte => Uint8Array.of(byte))]) {
      const { client } = createClient(() => sseResponse(parts));
      const events = [];
      for await (const event of client.stream()) events.push(event);
      assert.deepEqual(events, [{ event: "message", id: 1, data: original }]);
    }
  }
});

test("concurrent acknowledgments never move the stored cursor backward", async () => {
  let after = 0;
  const cursorStore = {
    load: () => after,
    async save(id) {
      if (id === 1) await new Promise(resolve => setImmediate(resolve));
      after = id;
    },
  };
  const { client } = createClient(() => json(page()), { cursorStore });
  assert.deepEqual(await Promise.all([
    client.acknowledge(goodMessage(1)), client.acknowledge(goodMessage(2)),
  ]), [true, true]);
  assert.equal(after, 2);
  assert.deepEqual(await Promise.all([
    client.acknowledge(goodMessage(3)), client.acknowledge(goodMessage(3)),
  ]), [true, false]);
  assert.equal(after, 3);
});

test("a failed cursor write does not block subsequent acknowledgments", async () => {
  let after = 0;
  const failure = new Error("synthetic write failure");
  const { client } = createClient(() => json(page()), { cursorStore: {
    load: () => after,
    async save(id) {
      if (id === 1) throw failure;
      after = id;
    },
  } });
  const failed = client.acknowledge(goodMessage(1));
  const next = client.acknowledge(goodMessage(2));
  await assert.rejects(failed, error => error === failure);
  assert.equal(await next, true);
  assert.equal(after, 2);
});

test("aborting SSE stops messages already buffered in the same chunk", async () => {
  const caller = new AbortController();
  const packet = [1, 2].map(id =>
    `id: ${id}\ndata: ${JSON.stringify(goodMessage(id))}\n\n`).join("");
  const { client } = createClient(() => sseResponse([new TextEncoder().encode(packet)]));
  const iterator = client.stream(0, caller.signal);
  assert.equal((await iterator.next()).value.id, 1);
  caller.abort();
  await errorKind(iterator.next(), "aborted");
  assert.equal((await iterator.next()).done, true);
});

test("timeout and cancellation bound an asynchronous token provider", async () => {
  for (const mode of ["timeout", "aborted"]) {
    const caller = new AbortController();
    let releaseToken;
    let entered;
    const started = new Promise(resolve => { entered = resolve; });
    const token = () => {
      entered();
      return new Promise(resolve => { releaseToken = resolve; });
    };
    const { client, calls } = createClient(() => json(page()), {
      token, timeoutMs: mode === "timeout" ? 5 : 1000,
    });
    const result = client.getInbox(0, { signal: caller.signal }).then(
      () => "unexpected success", error => error.kind);
    await started;
    if (mode === "aborted") caller.abort();
    let deadline;
    const outcome = await Promise.race([result, new Promise(resolve => {
      deadline = setTimeout(() => resolve("stalled"), 100);
    })]);
    clearTimeout(deadline);
    releaseToken("synthetic-late-token");
    await result;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(outcome, mode);
    assert.equal(calls.length, 0, "late token must not start a request");
  }
});

test("timeout configuration cannot overflow platform timers", async () => {
  for (const timeoutMs of [2147483648, Number.MAX_SAFE_INTEGER, Infinity, NaN, 0, -1]) {
    assert.throws(() => createClient(() => json(page()), { timeoutMs }),
      err => err instanceof BrokerError && err.kind === "configuration");
  }
  for (const timeoutMs of [1, 1000, 2147483647]) {
    const { client } = createClient(() => json(page()), { timeoutMs });
    assert.deepEqual(await client.getInbox(), page());
  }
});

test("SSE removes only one optional ASCII space from field values", async () => {
  const text = [
    "event:  message\nid: 1\ndata: " + JSON.stringify(goodMessage(1)) + "\n\n",
    "event:\tmessage\nid: 2\ndata: " + JSON.stringify(goodMessage(2)) + "\n\n",
    "event: message\nid: 3\ndata: " + JSON.stringify(goodMessage(3)) + "\n\n",
  ].join("");
  const { client } = createClient(() => new Response(text, {
    headers: { "Content-Type": "text/event-stream" },
  }));
  const received = [];
  for await (const event of client.stream()) received.push(event.id);
  assert.deepEqual(received, [3], "non-message event names must not be normalized into message");
});

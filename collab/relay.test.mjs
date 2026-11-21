import assert from 'node:assert/strict';
import { createDecipheriv, randomBytes } from 'node:crypto';
import { test } from 'node:test';
import { chromium } from 'playwright-core';
import { listen } from './relay.mjs';

async function shown(page, selector, text) {
  await page.locator(selector).getByText(text, { exact: true }).waitFor();
}

test('two browser sessions exchange AES-GCM events through an opaque loopback relay', { timeout: 30_000 }, async t => {
  const { server, url } = await listen(0);
  t.after(() => new Promise(resolve => {
    server.close(resolve);
    server.closeAllConnections();
  }));
  assert.equal(server.address().address, '127.0.0.1');
  const ingress = [];
  server.prependListener('request', req => {
    const record = { method: req.method, path: req.url, headers: req.rawHeaders, body: [] };
    ingress.push(record);
    req.on('data', chunk => record.body.push(chunk));
  });

  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  t.after(() => browser.close());
  // browser.newPage() gives each page a separate browser context.
  const a = await browser.newPage();
  const b = await browser.newPage();
  await a.goto(url);
  await a.getByRole('button', { name: 'Create room' }).click();
  await a.getByText('Connected', { exact: true }).waitFor();
  const invite = new URL(await a.getByLabel('Invite link').inputValue());
  const keyHex = invite.hash.slice(1);
  assert.match(keyHex, /^[0-9a-f]{64}$/);
  await b.goto(invite.href);
  await b.getByText('Connected', { exact: true }).waitFor();
  assert.equal(new URL(b.url()).hash, '');

  const room = invite.pathname.split('/').at(-1);
  const endpoint = `${url}/events/${room}`;
  // A third relay subscriber has the public room ID, but gets no key.
  const abort = new AbortController();
  t.after(() => abort.abort());
  const subscription = await fetch(endpoint, { signal: abort.signal });
  assert.equal(subscription.status, 200);
  const reader = subscription.body.pipeThrough(new TextDecoderStream()).getReader();
  let pending = '';
  async function nextEvent() {
    while (!pending.includes('\n\n')) {
      const { value, done } = await reader.read();
      assert.equal(done, false);
      pending += value;
    }
    const boundary = pending.indexOf('\n\n');
    const event = pending.slice(0, boundary);
    pending = pending.slice(boundary + 2);
    return event;
  }
  assert.equal(await nextEvent(), 'event: ready\ndata: ok');

  const text = `Room-only ${randomBytes(12).toString('hex')} ☂ <b>not markup</b>`;
  await a.getByLabel('Message').fill(text);
  await a.getByRole('button', { name: 'Send' }).click();
  await shown(b, '#events', text);
  assert.equal(await b.locator('#events b').count(), 0);
  const first = await nextEvent();

  await b.getByRole('button', { name: 'Wave' }).click();
  await shown(a, '#events', 'A participant waved.');
  await shown(b, '#events', 'A participant waved.');
  const second = await nextEvent();

  const posts = ingress.filter(record => record.method === 'POST');
  assert.equal(posts.length, 2);
  assert.deepEqual(posts.map(p => p.path), [`/events/${room}`, `/events/${room}`]);
  const frames = posts.map(p => Buffer.concat(p.body).toString());
  assert.deepEqual([first, second], frames.map(frame => `data: ${frame}`));
  const visible = JSON.stringify(ingress.map(record => ({ ...record, body: Buffer.concat(record.body).toString() })));
  assert.ok(!visible.includes(keyHex));
  assert.ok(!visible.includes(text));
  assert.equal(ingress.filter(record => record.path === invite.pathname).length, 2);

  const open = (wire, key) => {
    assert.match(wire, /^(?:[0-9a-f]{2})+$/);
    const frame = Buffer.from(wire, 'hex');
    assert.equal(frame.includes(Buffer.from(text)), false);
    assert.equal(frame.includes(Buffer.from(keyHex, 'hex')), false);
    const decipher = createDecipheriv('aes-256-gcm', key, frame.subarray(0, 12));
    decipher.setAAD(Buffer.from(`z0live-collab/event/v1/${room}`));
    decipher.setAuthTag(frame.subarray(-16));
    return JSON.parse(Buffer.concat([decipher.update(frame.subarray(12, -16)), decipher.final()]));
  };
  const key = Buffer.from(keyHex, 'hex');
  assert.deepEqual(frames.map(frame => open(frame, key)), [{ type: 'text', text }, { type: 'wave' }]);
  assert.throws(() => open(frames[0], randomBytes(32)));

  const count = await b.locator('#events li').count();
  const tampered = Buffer.from(frames[0], 'hex');
  tampered[tampered.length - 1] ^= 1;
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: { Origin: url, 'Content-Type': 'application/octet-stream' },
    body: tampered.toString('hex'),
  });
  assert.equal(response.status, 204); // The relay cannot detect the forgery.
  await b.getByText('Ignored an invalid encrypted event.', { exact: true }).waitFor();
  assert.equal(await b.locator('#events li').count(), count);
});

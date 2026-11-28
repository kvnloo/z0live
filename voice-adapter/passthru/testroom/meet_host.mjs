// Google Meet HOST side for passthru/e2e_meet.py: drives an already-running, signed-in Chrome over CDP
// (an agent-owned browser on a private display, never the owner's). It starts a meeting, admits z0,
// records what it hears from the other participants and leaves.
// Usage: node meet_host.mjs <cdp-url>       stdin: JSON lines {"cmd": ...}   stdout: JSON lines {"ev": ...}
//   create                 -> {"ev":"created","link":...}   (meet.google.com/new, then "Join now")
//   admit {secs, name}     -> {"ev":"admitted","clicks":n,"present":bool}  clicks Admit until `name` is in the call
//   record {secs, path}    -> {"ev":"recorded","path","bytes","tracks","rms"}  remote audio as webm/opus
//   shot {path}            -> {"ev":"shot"}
//   leave                  -> leaves the call, closes only the page it opened, detaches (browser keeps running)
import { createRequire } from 'module';
import { readdirSync, existsSync, writeFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';
import readline from 'readline';

const say = (ev, data = {}) => process.stdout.write(JSON.stringify({ ev, t: Date.now() / 1000, ...data }) + '\n');
function playwright() {
  const roots = [process.env.Z0_PLAYWRIGHT_DIR].filter(Boolean);
  const npx = join(homedir(), '.npm/_npx');
  if (existsSync(npx)) for (const d of readdirSync(npx)) roots.push(join(npx, d, 'node_modules'));
  for (const r of roots) for (const m of ['playwright', 'playwright-core']) { try { return createRequire(join(r, 'x.js'))(m); } catch (_) {} }
  throw new Error('playwright not found; set Z0_PLAYWRIGHT_DIR');
}

// Collect every remote audio track the Meet page receives, so `record` can mix them.
const TAP = `(() => { if (window.__hostTaps) return; window.__hostTaps = []; window.__hostPcs = [];
  const N = window.RTCPeerConnection; if (!N) return;
  const W = function (...a) { const pc = new N(...a); window.__hostPcs.push(pc); pc.addEventListener('track', e => { if (e.track.kind === 'audio') window.__hostTaps.push(e.track); }); return pc; };
  W.prototype = N.prototype; Object.setPrototypeOf(W, N); window.RTCPeerConnection = W;
  /* Meet plays the room through its own AudioContext / generated streams; tap those outputs too. */
  const nc = AudioNode.prototype.connect, outs = new WeakMap();
  AudioNode.prototype.connect = function (d, ...r) { const x = nc.call(this, d, ...r);
    try { if (d instanceof AudioDestinationNode && !this.context.__hostRec) { let md = outs.get(this.context);
      if (!md) { md = this.context.createMediaStreamDestination(); outs.set(this.context, md); window.__hostTaps.push(md.stream.getAudioTracks()[0]); }
      nc.call(this, md); } } catch (_) {} return x; };
  const sd = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', { ...sd, set(v) {
    try { if (v && v.getAudioTracks && !this.__hostRec) for (const t of v.getAudioTracks()) if (!window.__hostTaps.includes(t)) window.__hostTaps.push(t); } catch (_) {}
    return sd.set.call(this, v); } }); })();`;

// Audio RTP totals across the page's peer connections: does audio actually flow on the wire?
const RTP_STATS = `(async () => { const o = { in_bytes: 0, in_energy: 0, out_bytes: 0, out_energy: 0, pcs: 0 };
  for (const pc of (window.__hostPcs || (window.__z0passthru && window.__z0passthru.pcs) || [])) { if (pc.connectionState === 'closed') continue; o.pcs++;
    (await pc.getStats()).forEach(s => { if (s.kind !== 'audio') return;
      if (s.type === 'inbound-rtp') { o.in_bytes += s.bytesReceived || 0; o.in_energy += s.totalAudioEnergy || 0; }
      if (s.type === 'outbound-rtp') o.out_bytes += s.bytesSent || 0;
      if (s.type === 'media-source') o.out_energy += s.totalAudioEnergy || 0; });
    for (const r of pc.getReceivers()) if (r.track && r.track.kind === 'audio')
      for (const s of r.getSynchronizationSources()) o.in_level_max = Math.max(o.in_level_max || 0, s.audioLevel || 0);
    for (const s of pc.getSenders()) if (s.track && s.track.kind === 'audio') { o.mic_enabled = s.track.enabled; o.mic_muted = s.track.muted; } }
  o.in_energy = Math.round(o.in_energy * 1e4) / 1e4; o.out_energy = Math.round(o.out_energy * 1e4) / 1e4; return o; })()`;
const { chromium } = playwright();
const browser = await chromium.connectOverCDP(process.argv[2] || 'http://127.0.0.1:19377');
const ctx = browser.contexts()[0];
let page = null;

async function dismiss() {  // first-run tips ("Got it", "Dismiss") cover the controls
  for (const n of [/^Got it$/, /^Dismiss$/, /^Close$/]) await page.getByRole('button', { name: n }).first().click({ timeout: 800 }).catch(() => {});
}

async function create() {
  // A meeting tab left over from an aborted run would still be "in the call"; close those first.
  for (const p of ctx.pages()) if (/meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}/.test(p.url())) await p.close().catch(() => {});
  page = await ctx.newPage();
  await page.addInitScript({ content: TAP });
  await page.goto('https://meet.google.com/new', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForURL(/meet\.google\.com\/[a-z]{3}-[a-z]{4}-[a-z]{3}/, { timeout: 60000 });
  const link = page.url().replace(/\?.*$/, '');
  // meet.google.com/new usually drops a signed-in host straight into the call; older flows show "Join now".
  const leave = page.getByRole('button', { name: /Leave call/ }).first();
  if (!await leave.waitFor({ timeout: 15000 }).then(() => true, () => false)) {
    await page.getByRole('button', { name: /^(Join now|Start now|Join)$/ }).first().click({ timeout: 15000 });
    await leave.waitFor({ timeout: 30000 });
  }
  await dismiss();
  await page.getByRole('button', { name: /^Turn on microphone/ }).first().click({ timeout: 2000 }).catch(() => {});
  say('created', { link });
}

// z0 is in when its name shows, or (signed in as the host's own account, same display name) the
// participant counter reads 2 or more.
async function present(name) {
  const txt = await page.innerText('body').catch(() => '');
  if (name && txt.includes(name)) return true;
  const btns = await page.getByRole('button').allInnerTexts().catch(() => []);
  return btns.some(b => /^\d+$/.test(b.trim()) && Number(b.trim()) >= 2);
}

// A signed-in identity for z0: this browser's Google cookies as a Playwright storageState (0600).
async function state(path) {
  const cookies = (await ctx.cookies()).filter(c => c.domain.replace(/^\./, '').endsWith('google.com'));
  writeFileSync(path, JSON.stringify({ cookies, origins: [] }), { mode: 0o600 });
  say('state', { cookies: cookies.length });
}

async function admit(secs = 90, name = 'z0') {
  const end = Date.now() + secs * 1000; let clicks = 0;
  while (Date.now() < end) {
    for (const n of [/^Admit( all| \d+ guests?)?$/, /^Admit$/]) {
      const b = page.getByRole('button', { name: n }).first();
      if (await b.isVisible().catch(() => false)) { await b.click({ timeout: 3000 }).catch(() => {}); clicks++; say('admit.click', { clicks }); }
    }
    // A collapsed request shows "View" first; open it so the Admit button appears.
    const v = page.getByRole('button', { name: /^View( all)?$/ }).first();
    if (await v.isVisible().catch(() => false)) await v.click({ timeout: 2000 }).catch(() => {});
    if (await present(name)) { await dismiss(); say('admitted', { clicks, present: true }); return; }
    await page.waitForTimeout(700);
  }
  say('admitted', { clicks, present: await present(name) });
}

async function record(secs, path) {
  const r = await page.evaluate(async secs => {
    const live = (window.__hostTaps || []).filter(t => t.readyState === 'live');
    const ctx = new AudioContext({ sampleRate: 48000 }); ctx.__hostRec = true; await ctx.resume().catch(() => {});
    const dest = ctx.createMediaStreamDestination(), an = ctx.createAnalyser(); an.fftSize = 2048;
    for (const t of live) {
      const s = new MediaStream([t]); const el = new Audio(); el.__hostRec = true; el.muted = true; el.srcObject = s; el.play().catch(() => {});
      const src = ctx.createMediaStreamSource(s); src.connect(dest); src.connect(an);
    }
    // Record each remote track directly too: WebAudio may not run on a host with no sound device.
    const per = live.map(t => { const r = new MediaRecorder(new MediaStream([t]), { mimeType: 'audio/webm;codecs=opus' }); r.chunks = []; r.ondataavailable = e => r.chunks.push(e.data); r.start(250); return r; });
    const rec = new MediaRecorder(dest.stream, { mimeType: 'audio/webm;codecs=opus' }); rec.chunks = [];
    rec.ondataavailable = e => rec.chunks.push(e.data); rec.start(250);
    let peak = 0; const buf = new Float32Array(2048);
    const iv = setInterval(() => { an.getFloatTimeDomainData(buf); let e = 0; for (const x of buf) e += x * x; peak = Math.max(peak, Math.sqrt(e / buf.length)); }, 50);
    await new Promise(r => setTimeout(r, secs * 1000));
    clearInterval(iv);
    const all = [rec, ...per];
    await Promise.all(all.map(r => { const p = new Promise(ok => { r.onstop = ok; }); r.stop(); return p; })); const ctxState = ctx.state; ctx.close();
    const enc = async r => { const bytes = new Uint8Array(await new Blob(r.chunks).arrayBuffer()); let s = '';
      for (let i = 0; i < bytes.length; i += 32768) s += String.fromCharCode(...bytes.subarray(i, i + 32768)); return btoa(s); };
    return { tracks: live.length, ctx: ctxState, peak_rms: Math.round(peak * 1e4) / 1e4, b64: await enc(rec), per: await Promise.all(per.map(enc)) };
  }, secs);
  const data = Buffer.from(r.b64, 'base64'); writeFileSync(path, data);
  const per = r.per.map((b, i) => { const d = Buffer.from(b, 'base64'); const p = path.replace(/\.webm$/, `.track${i}.webm`); writeFileSync(p, d); return [p, d.length]; });
  say('recorded', { path, bytes: data.length, tracks: r.tracks, ctx: r.ctx, peak_rms: r.peak_rms, per });
}

async function leave() {
  if (page) {
    await page.getByRole('button', { name: /Leave call/ }).first().click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(1000); await page.close().catch(() => {});
  }
  say('left'); await browser.close().catch(() => {});  // over CDP this detaches; the host browser keeps running
  process.exit(0);
}

readline.createInterface({ input: process.stdin }).on('line', async line => {
  let m; try { m = JSON.parse(line); } catch (_) { return; }
  try {
    if (m.cmd === 'create') await create();
    else if (m.cmd === 'admit') await admit(m.secs, m.name);
    else if (m.cmd === 'state') await state(m.path);
    else if (m.cmd === 'rtp') say('rtp', { audio: await page.evaluate(RTP_STATS) });
    else if (m.cmd === 'record') await record(m.secs, m.path);
    else if (m.cmd === 'shot') { await page.screenshot({ path: m.path }); say('shot', { path: m.path }); }
    else if (m.cmd === 'leave') await leave();
  } catch (e) { say('cmd.failed', { cmd: m.cmd, detail: String(e).slice(0, 300) }); }
}).on('close', leave);
say('ready', { pages: ctx.pages().length });

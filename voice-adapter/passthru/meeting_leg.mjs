// z0live passthru meeting leg: a browser on the Studio that joins Kevin's other call as "z0".
// Usage: node meeting_leg.mjs '{"url":..., "ws":..., "token":..., "name":..., "headless":true}'
// stdin: JSON lines {"cmd":"share","url":...} | {"cmd":"unshare"} | {"cmd":"leave"} | {"cmd":"status"}
// stdout: JSON lines {"ev":...} for the adapter's trace.
import { createRequire } from 'module';
import { readdirSync, readFileSync, existsSync } from 'fs';
import { homedir } from 'os';
import { dirname, join } from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import readline from 'readline';

const here = dirname(fileURLToPath(import.meta.url));
const cfg = JSON.parse(process.argv[2] || '{}');
const say = (ev, data = {}) => process.stdout.write(JSON.stringify({ ev, t: Date.now() / 1000, ...data }) + '\n');

function playwright() {
  const roots = [process.env.Z0_PLAYWRIGHT_DIR, join(here, 'node_modules')].filter(Boolean);
  const npx = join(homedir(), '.npm/_npx');
  if (existsSync(npx)) for (const d of readdirSync(npx)) roots.push(join(npx, d, 'node_modules'));
  for (const r of roots) { try { return createRequire(join(r, 'x.js'))('playwright'); } catch (_) {} }
  throw new Error('playwright not found; set Z0_PLAYWRIGHT_DIR');
}

// Recipes are the extension point: one file per service in ./recipes, plus $Z0LIVE_PASSTHRU_RECIPES.
async function loadRecipes() {
  const dirs = [join(here, 'recipes'), ...(process.env.Z0LIVE_PASSTHRU_RECIPES || '').split(':').filter(Boolean)];
  const out = []; let generic = null;
  for (const dir of dirs) {
    if (!existsSync(dir)) continue;
    for (const f of readdirSync(dir).filter(f => f.endsWith('.mjs')).sort()) {
      const r = (await import(pathToFileURL(join(dir, f)).href)).default;
      if (r && r.name === 'generic') generic = r; else if (r && r.match) out.push(r);
    }
  }
  return [...out, generic || { name: 'generic', match: /./, async join() {} }];
}
const ALL = await loadRecipes();

const { chromium } = playwright();
// cfg.create = recipe name: start a new meeting on that service instead of joining a link.
const recipe = cfg.create ? ALL.find(r => r.name === cfg.create) : ALL.find(r => r.match.test(cfg.url));
if (!recipe || (cfg.create && !recipe.create)) { say('join.failed', { detail: 'no recipe for ' + (cfg.create || cfg.url) }); process.exit(2); }
const url = cfg.create ? recipe.home : (recipe.rewrite ? recipe.rewrite(cfg.url) : cfg.url);
const browser = await chromium.launch({
  headless: cfg.headless !== false,
  args: [
    '--autoplay-policy=no-user-gesture-required',
    '--use-fake-ui-for-media-stream',               // grant mic/camera without a prompt; our init script supplies the streams
    '--enable-features=WebRtcHideLocalIpsWithMdns',
    // inject.js talks to the adapter on loopback; public meeting sites are otherwise barred from it
    // (Local/Private Network Access). Only this throwaway z0 profile gets the exemption.
    '--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults',
  ],
});
const context = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  permissions: ['microphone', 'camera'],
  bypassCSP: true,   // the app's CSP would block inject.js's loopback socket to the adapter
  userAgent: cfg.userAgent || undefined,
});
await context.grantPermissions(['microphone', 'camera'], { origin: new URL(url).origin }).catch(() => {});
await context.addInitScript({ content: `window.__Z0_PASSTHRU_CFG__ = ${JSON.stringify({ ws: cfg.ws, token: cfg.token, name: cfg.name })};\n` + readFileSync(join(here, 'inject.js'), 'utf8') });

let sharePage = null;
const page = await context.newPage();
page.on('pageerror', e => say('page.error', { detail: String(e).slice(0, 300) }));
page.on('close', () => { say('left', { reason: 'page_closed' }); shutdown(); });

async function stats() {
  return page.evaluate(() => { const s = window.__z0passthru; return s ? { sent: s.sent, recv: s.recv, pcs: s.pcs.length, ws: s.ws && s.ws.readyState } : null; }).catch(() => null);
}

let cast = null;
async function share(target) {
  if (!sharePage) {
    sharePage = await context.newPage();
    await sharePage.setViewportSize({ width: 1280, height: 720 });
    sharePage.on('close', () => { sharePage = null; });
  }
  await sharePage.goto(target, { waitUntil: 'domcontentloaded', timeout: 20000 });
  if (!cast) {
    cast = await context.newCDPSession(sharePage);
    let frames = 0;
    cast.on('Page.screencastFrame', async f => {
      cast.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {});
      frames++;
      await page.evaluate(b64 => window.__z0passthru && window.__z0passthru.frame(b64), f.data).catch(() => {});
    });
    await cast.send('Page.startScreencast', { format: 'jpeg', quality: 70, maxWidth: 1280, maxHeight: 720, everyNthFrame: 1 });
  }
  await page.waitForFunction(() => (window.__z0passthru.frames || 0) > 0, null, { timeout: 10000 }).catch(() => {});
  if (recipe.share) {
    try { await recipe.share(page); say('share.started', { via: 'app', target }); return; }
    catch (e) { say('share.app_failed', { detail: String(e).slice(0, 200) }); }
  }
  // No app recipe (or its button moved): z0's camera tile shows the share surface. Works in any app that shows video.
  await page.evaluate(() => window.__z0passthru.startCameraShare());
  say('share.started', { via: 'camera', target });
}

async function unshare() {
  if (recipe.unshare) await recipe.unshare(page).catch(() => {});
  if (cast) { await cast.send('Page.stopScreencast').catch(() => {}); await cast.detach().catch(() => {}); cast = null; }
  await page.evaluate(() => window.__z0passthru && window.__z0passthru.stopShare()).catch(() => {});
  if (sharePage) { await sharePage.close().catch(() => {}); sharePage = null; }
  say('share.stopped');
}

let closing = false;
async function shutdown() {
  if (closing) return; closing = true;
  if (recipe.leave) await recipe.leave(page).catch(() => {});
  await browser.close().catch(() => {});
  say('closed');
  process.exit(0);
}
process.on('SIGTERM', shutdown);

readline.createInterface({ input: process.stdin }).on('line', async line => {
  let m; try { m = JSON.parse(line); } catch (_) { return; }
  try {
    if (m.cmd === 'share') await share(m.url);
    else if (m.cmd === 'unshare') await unshare();
    else if (m.cmd === 'leave') await shutdown();
    else if (m.cmd === 'status') say('status', { stats: await stats(), sharing: !!sharePage });
    else if (m.cmd === 'eval' && process.env.Z0LIVE_PASSTHRU_DEBUG) say('eval', { out: String(await page.evaluate(m.js)).slice(0, 800) });
  } catch (e) { say('cmd.failed', { cmd: m.cmd, detail: String(e).slice(0, 300) }); }
}).on('close', shutdown);

say('opening', { url: cfg.create ? 'new ' + recipe.name : url, recipe: recipe.name });
try {
  if (cfg.create) { const link = await recipe.create(page, cfg); say('created', { link }); }
  else await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await recipe.join(page, cfg); say('joined', { recipe: recipe.name });
}
catch (e) { say('join.failed', { detail: String(e).slice(0, 300) }); }
if (cfg.share) await share(cfg.share).catch(e => say('share.failed', { detail: String(e).slice(0, 200) }));
setInterval(async () => say('stats', { stats: await stats() }), 5000);

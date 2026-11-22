// A real, uninjected guest on call.element.io: speaks 440 Hz, reports what it hears and sees from z0.
// Usage: node element_guest.mjs '<link>' <secs>
import { createRequire } from 'module'; import { readdirSync } from 'fs'; import { homedir } from 'os'; import { join } from 'path';
const npx = join(homedir(), '.npm/_npx'); let pw;
for (const d of readdirSync(npx)) { try { pw = createRequire(join(npx, d, 'node_modules', 'x.js'))('playwright'); break; } catch (_) {} }
const [link, secs] = [process.argv[2], Number(process.argv[3] || 40)];
const b = await pw.chromium.launch({ args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', '--autoplay-policy=no-user-gesture-required'] });
const ctx = await b.newContext({ permissions: ['microphone', 'camera'], viewport: { width: 1280, height: 800 } });
await ctx.addInitScript(() => {   // measurement only: a 440 Hz mic, and an analyser on received audio
  const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  let ac; const A = () => ac || (ac = new AudioContext({ sampleRate: 48000 }));
  navigator.mediaDevices.getUserMedia = async c => {
    const s = c && c.video ? await gum({ video: c.video }) : new MediaStream();
    if (c && c.audio) { const o = A().createOscillator(); o.frequency.value = 440; const g = A().createGain(); g.gain.value = 0.3;
      const d = A().createMediaStreamDestination(); o.connect(g); g.connect(d); o.start(); s.addTrack(d.stream.getAudioTracks()[0]); }
    return s;
  };
  const Native = window.RTCPeerConnection;
  window.__an = null;
  window.RTCPeerConnection = function (...a) {
    const pc = new Native(...a);
    pc.addEventListener('track', e => { if (e.track.kind !== 'audio') return;
      const el = new Audio(); el.muted = true; el.srcObject = new MediaStream([e.track]); el.play().catch(() => {});
      if (!window.__an) { window.__an = A().createAnalyser(); window.__an.fftSize = 8192; }
      A().resume(); A().createMediaStreamSource(new MediaStream([e.track])).connect(window.__an); });
    return pc;
  };
  window.RTCPeerConnection.prototype = Native.prototype;
});
const p = await ctx.newPage();
await p.goto(link);
const tid = id => p.locator(`[data-testid="${id}"]`);
if (await tid('joincall_joincall').waitFor({ state: "visible", timeout: 15000 }).then(() => true, () => false)) { await p.getByLabel('Display name').fill('Ana'); await tid('joincall_joincall').click(); }
await tid('lobby_joinCall').click({ timeout: 30000 });
for (let i = 0; i < secs * 2; i++) {
  await p.waitForTimeout(500);
  console.log(JSON.stringify(await p.evaluate(() => {
    const out = { encrypted: /Encrypted/.test(document.body.innerText), heardHz: 0, heardDb: -200 };
    if (window.__an) { const bins = new Float32Array(window.__an.frequencyBinCount); window.__an.getFloatFrequencyData(bins); let k = 0;
      for (let j = 1; j < bins.length; j++) if (bins[j] > bins[k]) k = j; out.heardHz = Math.round(k * 48000 / 8192); out.heardDb = Math.round(bins[k]); }
    const c = document.createElement('canvas'); c.width = 64; c.height = 36; const g = c.getContext('2d');
    out.videos = [...document.querySelectorAll('video')].filter(v => v.videoWidth).map(v => { g.drawImage(v, 0, 0, 64, 36);
      const d = g.getImageData(32, 18, 1, 1).data; return [v.videoWidth, v.videoHeight, d[0], d[1], d[2]]; });
    out.magentaVideo = out.videos.some(v => v[2] > 200 && v[3] < 60 && v[4] > 200);
    return out;
  })));
}
await b.close();

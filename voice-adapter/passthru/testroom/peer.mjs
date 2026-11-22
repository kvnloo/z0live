// Drives the uninjected "other participant" browser; prints its measurements as JSON lines.
import { createRequire } from 'module'; import { readdirSync } from 'fs'; import { homedir } from 'os'; import { join } from 'path';
const npx = join(homedir(), '.npm/_npx'); let pw;
for (const d of readdirSync(npx)) { try { pw = createRequire(join(npx, d, 'node_modules', 'x.js'))('playwright'); break; } catch (_) {} }
const b = await pw.chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
const p = await b.newPage(); await p.goto(process.argv[2]);
const secs = Number(process.argv[3] || 30);
for (let i = 0; i < secs * 2; i++) { await p.waitForTimeout(500); console.log(JSON.stringify(await p.evaluate(() => window.result))); }
await b.close();

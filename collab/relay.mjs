import { once } from 'node:events';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { pathToFileURL } from 'node:url';

const host = '127.0.0.1';
const html = readFileSync(new URL('./index.html', import.meta.url));
const script = readFileSync(new URL('./client.js', import.meta.url));
const roomPage = /^\/r\/[0-9a-f]{32}$/;
const eventPath = /^\/events\/([0-9a-f]{32})$/;
const maxEnvelope = 8192;

export async function listen(port = 8788) {
  // The relay's only room state is its live subscribers. No keys or history.
  const rooms = new Map();
  const server = createServer({ requestTimeout: 10_000, headersTimeout: 5_000 }, (req, res) => {
    void route(req, res).catch(() => res.destroy());
  });
  server.maxConnections = 64;
  server.listen(port, host);
  await once(server, 'listening');
  const authority = `${host}:${server.address().port}`;
  const url = `http://${authority}`;
  return { server, url };

  async function route(req, res) {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
    const end = (status, type, body) => {
      res.statusCode = status;
      if (type) res.setHeader('Content-Type', type);
      res.end(body);
    };
    if (req.headers.host !== authority || (req.headers.origin && req.headers.origin !== url)) return end(403);

    if (req.method === 'GET') {
      if (req.url === '/' || roomPage.test(req.url)) return end(200, 'text/html; charset=utf-8', html);
      if (req.url === '/client.js') return end(200, 'text/javascript; charset=utf-8', script);
    }
    const room = eventPath.exec(req.url)?.[1];
    if (!room) return end(404);

    if (req.method === 'GET') {
      let subscribers = rooms.get(room);
      if (!subscribers) rooms.set(room, subscribers = new Set());
      subscribers.add(res);
      res.on('close', () => {
        subscribers.delete(res);
        if (!subscribers.size) rooms.delete(room);
      });
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('event: ready\ndata: ok\n\n');
      return;
    }

    if (req.method !== 'POST') return end(405);
    if (req.headers.origin !== url) return end(403);
    if (req.headers['content-type'] !== 'application/octet-stream') return end(415);
    const size = Number(req.headers['content-length']);
    if (!Number.isInteger(size) || size < 58 || size > maxEnvelope) return end(400);
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const frame = Buffer.concat(chunks).toString('latin1');
    if (frame.length !== size || !/^(?:[0-9a-f]{2})+$/.test(frame)) return end(400);

    // No decryption or event parsing. Hex also prevents SSE delimiter injection.
    for (const subscriber of rooms.get(room) ?? []) {
      if (!subscriber.write(`data: ${frame}\n\n`)) subscriber.destroy();
    }
    end(204);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { url } = await listen(Number(process.argv[2] ?? 8788));
  console.log(`Collab prototype: ${url}`);
}

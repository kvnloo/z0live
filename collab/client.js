const el = id => document.getElementById(id);
const random = size => crypto.getRandomValues(new Uint8Array(size));
const hex = bytes => Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
const unhex = value => Uint8Array.from(value.match(/../g), pair => parseInt(pair, 16));
const utf8 = new TextEncoder();
const decode = new TextDecoder('utf-8', { fatal: true });

el('create').onclick = () => {
  location.assign(`/r/${hex(random(16))}#${hex(random(32))}`);
};

const room = /^\/r\/([0-9a-f]{32})$/.exec(location.pathname)?.[1];
const invite = location.href;
const secret = location.hash.slice(1);
if (location.hash) history.replaceState(null, '', location.pathname);
if (room && /^[0-9a-f]{64}$/.test(secret) && crypto.subtle) {
  const key = await crypto.subtle.importKey('raw', unhex(secret), 'AES-GCM', false, ['encrypt', 'decrypt']);
  join(room, invite, key);
} else if (room) {
  el('status').textContent = 'Open the complete invite link, including its key, on loopback.';
}

function join(room, invite, key) {
  const endpoint = `/events/${room}`;
  const additionalData = utf8.encode(`z0live-collab/event/v1/${room}`);
  const params = iv => ({ name: 'AES-GCM', iv, additionalData, tagLength: 128 });
  el('create').hidden = true;
  el('invite').value = invite;
  el('room').hidden = false;
  el('status').textContent = 'Connecting';
  const stream = new EventSource(endpoint);
  stream.addEventListener('ready', () => {
    el('status').textContent = 'Connected';
    el('controls').disabled = false;
  });
  stream.onerror = () => {
    el('status').textContent = 'Disconnected. Reconnecting…';
    el('controls').disabled = true;
  };
  // Preserve delivery order even if Web Crypto finishes out of order.
  let received = Promise.resolve();
  stream.onmessage = ({ data }) => {
    received = received.then(async () => {
      try {
        if (data.length < 58 || data.length > 8192 || !/^(?:[0-9a-f]{2})+$/.test(data)) throw new Error();
        const frame = unhex(data);
        const plain = await crypto.subtle.decrypt(params(frame.subarray(0, 12)), key, frame.subarray(12));
        const event = JSON.parse(decode.decode(plain));
        const item = document.createElement('li');
        if (event?.type === 'text' && typeof event.text === 'string' && event.text.length <= 500) {
          item.textContent = event.text;
        } else if (event?.type === 'wave') {
          item.textContent = 'A participant waved.';
        } else {
          throw new Error();
        }
        el('events').append(item);
      } catch {
        el('notice').textContent = 'Ignored an invalid encrypted event.';
      }
    });
  };

  async function send(event) {
    try {
      const iv = random(12);
      const encrypted = new Uint8Array(await crypto.subtle.encrypt(params(iv), key, utf8.encode(JSON.stringify(event))));
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: hex(iv) + hex(encrypted),
      });
      if (!response.ok) throw new Error();
      el('notice').textContent = '';
      return true;
    } catch {
      el('notice').textContent = 'Event not sent.';
      return false;
    }
  }

  el('compose').onsubmit = async event => {
    event.preventDefault();
    const text = el('text').value;
    if (text.trim() && await send({ type: 'text', text }) && el('text').value === text) el('text').value = '';
  };
  el('wave').onclick = () => send({ type: 'wave' });
}

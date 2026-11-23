// z0live passthru: injected into every page of the meeting browser before any page script runs.
// It swaps the page's microphone for z0's voice and taps the other participants' audio, so any
// WebRTC app (Meet, Zoom web, Teams, Jitsi, whiteboards, custom canvases) works without a
// virtual audio driver. Wire format on the loopback socket: binary = Int16 mono 48 kHz PCM,
// browser->adapter is the room mix, adapter->browser is z0's voice. Text = JSON status.
(cfg => {
  if (window.__z0passthru) return;
  const RATE = 48000;
  const state = { ws: null, ctx: null, mic: null, sink: null, tapped: new WeakSet(), pcs: [], sent: 0, recv: 0, share: null };
  window.__z0passthru = state;

  const WORKLET = `
    class Z0Src extends AudioWorkletProcessor {
      constructor() { super(); this.q = []; this.off = 0; this.port.onmessage = e => { if (this.q.length < 50) this.q.push(e.data); }; }
      process(_, outs) {
        const out = outs[0][0];
        for (let i = 0; i < out.length; i++) {
          if (!this.q.length) { out.fill(0, i); break; }
          const b = this.q[0]; out[i] = b[this.off++];
          if (this.off >= b.length) { this.q.shift(); this.off = 0; }
        }
        return true;
      }
    }
    class Z0Sink extends AudioWorkletProcessor {
      constructor() { super(); this.buf = new Float32Array(960); this.n = 0; }
      process(ins) {
        const ch = ins[0] && ins[0][0];
        if (!ch) return true;
        for (let i = 0; i < ch.length; i++) {
          this.buf[this.n++] = ch[i];
          if (this.n === 960) { this.port.postMessage(this.buf); this.buf = new Float32Array(960); this.n = 0; }
        }
        return true;
      }
    }
    registerProcessor('z0-src', Z0Src); registerProcessor('z0-sink', Z0Sink);`;

  let ready = null;
  function audio() {
    if (ready) return ready;
    ready = (async () => {
      const ctx = new AudioContext({ sampleRate: RATE });
      const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      await ctx.audioWorklet.addModule(url);
      const mic = new AudioWorkletNode(ctx, 'z0-src', { numberOfOutputs: 1, outputChannelCount: [1] });
      const dest = ctx.createMediaStreamDestination();
      mic.connect(dest);
      const sink = new AudioWorkletNode(ctx, 'z0-sink', { numberOfInputs: 1, channelCount: 1, channelCountMode: 'explicit' });
      const mute = ctx.createGain(); mute.gain.value = 0; sink.connect(mute); mute.connect(ctx.destination);  // keeps the sink pulled
      sink.port.onmessage = e => {
        const f = e.data, ws = state.ws;
        if (!ws || ws.readyState !== 1) return;
        const pcm = new Int16Array(f.length);
        for (let i = 0; i < f.length; i++) pcm[i] = Math.max(-1, Math.min(1, f[i])) * 32767;
        ws.send(pcm.buffer); state.sent++;
      };
      Object.assign(state, { ctx, mic, dest, sink });
      if (ctx.state !== 'running') ctx.resume().catch(() => {});
      connect();
      return state;
    })();
    return ready;
  }

  function connect() {
    if (!cfg.ws || (state.ws && state.ws.readyState <= 1)) return;
    const ws = new WebSocket(cfg.ws);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => ws.send(JSON.stringify({ type: 'hello', token: cfg.token, href: location.href, frame: window === top ? 'top' : 'sub' }));
    ws.onmessage = e => {
      if (typeof e.data === 'string') return;
      const pcm = new Int16Array(e.data), f = new Float32Array(pcm.length);
      for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768;
      state.recv++;
      let e2 = 0; for (let i = 0; i < f.length; i += 8) e2 += f[i] * f[i];
      state.recvLevel = Math.sqrt(e2 / (f.length / 8));
      state.mic && state.mic.port.postMessage(f, [f.buffer]);
    };
    ws.onclose = () => setTimeout(connect, 1000);
    state.ws = ws;
  }

  // ---- other participants: tap every remote audio track the page receives
  function tap(track) {
    if (track.kind !== 'audio' || state.tapped.has(track)) return;
    state.tapped.add(track);
    audio().then(({ ctx, sink }) => {
      const stream = new MediaStream([track]);
      const el = new Audio(); el.muted = true; el.srcObject = stream; el.play().catch(() => {});  // Chrome only pulls remote WebRTC audio into WebAudio while an element plays it
      const src = ctx.createMediaStreamSource(stream);
      src.connect(sink);
      track.addEventListener('ended', () => { try { src.disconnect(); } catch (_) {} });
    });
  }
  const NativePC = window.RTCPeerConnection;
  if (NativePC) {
    const Wrapped = function (...args) {
      const pc = new NativePC(...args);
      state.pcs.push(pc);
      pc.addEventListener('track', e => tap(e.track));
      audio();
      return pc;
    };
    Wrapped.prototype = NativePC.prototype;
    Object.setPrototypeOf(Wrapped, NativePC);
    window.RTCPeerConnection = Wrapped;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Wrapped;
  }

  // ---- share surface: the controller screencasts the share page (canvas, whiteboard, phone mirror)
  // and pushes JPEG frames here; apps get this canvas as the "screen", so no OS capture is needed.
  function surface() {
    if (state.surface) return state.surface;
    const c = document.createElement('canvas'); c.width = 1280; c.height = 720;
    const g = c.getContext('2d'); g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height);
    const img = new Image(); let pending = false;
    img.onload = () => { const k = Math.min(c.width / img.width, c.height / img.height), w = img.width * k, h = img.height * k;
      g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, (c.width - w) / 2, (c.height - h) / 2, w, h); pending = false; };
    const stream = c.captureStream(15);
    // Chrome only emits canvas frames on draws; repaint a pixel so a static page still streams.
    setInterval(() => { const p = g.getImageData(0, 0, 1, 1); g.putImageData(p, 0, 0); }, 200);
    state.surface = { c, stream, push(b64) { if (pending) return; pending = true; img.src = 'data:image/jpeg;base64,' + b64; } };
    return state.surface;
  }
  state.frame = b64 => { surface().push(b64); state.frames = (state.frames || 0) + 1; };
  state.startCameraShare = () => { state.share = true; };
  state.stopShare = () => { state.share = false; if (state.surface) { state.surface.stream.getTracks().forEach(t => t.stop()); state.surface = null; } };

  // ---- z0's camera: a small animated tile that pulses while z0 speaks
  let cam = null;
  function camera() {
    if (cam) return cam;
    const c = document.createElement('canvas'); c.width = 640; c.height = 360;
    const g = c.getContext('2d'); let t = 0;
    const draw = () => {
      t += 1; const level = Math.min(1, (state.recvLevel || 0) * 4);
      state.recvLevel = (state.recvLevel || 0) * 0.8;
      if (state.share && state.surface) {  // camera-swap share: the tile shows the share surface instead of the avatar
        if (c.width !== 1280) { c.width = 1280; c.height = 720; }
        g.drawImage(state.surface.c, 0, 0); return;
      } else if (c.width !== 640) { c.width = 640; c.height = 360; }
      g.fillStyle = '#0b0d12'; g.fillRect(0, 0, 640, 360);
      g.beginPath(); g.arc(320, 170, 70 + level * 30 + Math.sin(t / 20) * 3, 0, Math.PI * 2);
      g.fillStyle = `rgba(120,170,255,${0.35 + level * 0.5})`; g.fill();
      g.fillStyle = '#e8ecf4'; g.font = '600 44px system-ui, sans-serif'; g.textAlign = 'center'; g.fillText('z0', 320, 186);
      g.font = '20px system-ui, sans-serif'; g.fillStyle = '#8b93a7'; g.fillText(cfg.name || "z0 assistant", 320, 300);
    };
    setInterval(draw, 66); draw();
    cam = c.captureStream(15);
    return cam;
  }

  const md = navigator.mediaDevices;
  if (md) {
    const nativeGUM = md.getUserMedia.bind(md);
    md.getUserMedia = async constraints => {
      const out = new MediaStream();
      if (constraints && constraints.audio) { const s = await audio(); out.addTrack(s.dest.stream.getAudioTracks()[0].clone()); }
      if (constraints && constraints.video) out.addTrack(camera().getVideoTracks()[0].clone());
      if (!out.getTracks().length) return nativeGUM(constraints);
      return out;
    };
    md.enumerateDevices = async () => [
      { deviceId: 'z0-mic', kind: 'audioinput', label: 'z0 voice', groupId: 'z0', toJSON() { return this; } },
      { deviceId: 'z0-cam', kind: 'videoinput', label: 'z0 camera', groupId: 'z0', toJSON() { return this; } },
      { deviceId: 'z0-out', kind: 'audiooutput', label: 'z0 ears', groupId: 'z0', toJSON() { return this; } },
    ];
    // Screen share: whatever the app's own share button asks for, it gets z0's share surface.
    md.getDisplayMedia = async () => { const st = surface().stream; state.share = false; return new MediaStream(st.getVideoTracks().map(t => t.clone())); };
  }
})(window.__Z0_PASSTHRU_CFG__ || {});

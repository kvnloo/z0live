import Recorder from "opus-recorder";
import "./style.css";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const statusEl = $<HTMLParagraphElement>("status");
const connectButton = $<HTMLButtonElement>("connect");
const disconnectButton = $<HTMLButtonElement>("disconnect");
const gatewayInput = $<HTMLInputElement>("gateway");
const transcriptEl = $<HTMLDivElement>("transcript");
const floorEl = $<HTMLSpanElement>("floor");
const micEl = $<HTMLElement>("mic");
const queueEl = $<HTMLElement>("queue");
const codecEl = $<HTMLElement>("codec");

const defaultHost = window.location.hostname || "127.0.0.1";
gatewayInput.value = localStorage.getItem("z0live.gateway") || `ws://${defaultHost}:8765`;

let ws: WebSocket | null = null;
let recorder: any = null;
let decoder: Worker | null = null;
let audioContext: AudioContext | null = null;
let micStream: MediaStream | null = null;
let analyser: AnalyserNode | null = null;
let vadFrame = 0;
let speechActive = false;
let belowSince = 0;
let playbackAt = 0;
let assistantSpeechActive = false;
let assistantBelowSince = 0;
let traceId = crypto.randomUUID();

function setStatus(text: string): void {
  statusEl.textContent = text;
}

function sendEvent(kind: string, payload: Record<string, unknown> = {}): void {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    type: "event",
    event: {
      kind,
      source: "z0live-web",
      trace_id: traceId,
      payload,
    },
  }));
}

function startVad(stream: MediaStream): void {
  if (!audioContext) return;
  const source = audioContext.createMediaStreamSource(stream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);
  const samples = new Float32Array(analyser.fftSize);

  const tick = () => {
    if (!analyser) return;
    analyser.getFloatTimeDomainData(samples);
    let energy = 0;
    for (const sample of samples) energy += sample * sample;
    const rms = Math.sqrt(energy / samples.length);
    const now = performance.now();

    if (!speechActive && rms >= 0.018) {
      speechActive = true;
      belowSince = 0;
      floorEl.textContent = "you";
      sendEvent("user.speech.started", { rms });
    } else if (speechActive) {
      if (rms < 0.010) {
        if (!belowSince) belowSince = now;
        if (now - belowSince >= 260) {
          speechActive = false;
          belowSince = 0;
          floorEl.textContent = "idle";
          sendEvent("user.speech.stopped", { rms });
        }
      } else {
        belowSince = 0;
      }
    }
    vadFrame = requestAnimationFrame(tick);
  };
  vadFrame = requestAnimationFrame(tick);
}

function stopVad(): void {
  if (vadFrame) cancelAnimationFrame(vadFrame);
  vadFrame = 0;
  analyser?.disconnect();
  analyser = null;
  if (speechActive) sendEvent("user.speech.stopped", { reason: "disconnect" });
  speechActive = false;
}

function observeAssistantPcm(pcm: Float32Array): void {
  let energy = 0;
  for (const sample of pcm) energy += sample * sample;
  const rms = Math.sqrt(energy / Math.max(1, pcm.length));
  const now = performance.now();

  if (!assistantSpeechActive && rms >= 0.012) {
    assistantSpeechActive = true;
    assistantBelowSince = 0;
    floorEl.textContent = speechActive ? "overlap" : "assistant";
    sendEvent("assistant.speech.started", { rms, observed_at: "playback" });
  } else if (assistantSpeechActive) {
    if (rms < 0.006) {
      if (!assistantBelowSince) assistantBelowSince = now;
      if (now - assistantBelowSince >= 240) {
        assistantSpeechActive = false;
        assistantBelowSince = 0;
        floorEl.textContent = speechActive ? "you" : "idle";
        sendEvent("assistant.speech.stopped", { rms, observed_at: "playback" });
      }
    } else {
      assistantBelowSince = 0;
    }
  }
}

function initDecoder(): void {
  if (!audioContext) throw new Error("audio context missing");
  decoder = new Worker("/assets/decoderWorker.min.js");
  decoder.postMessage({
    command: "init",
    bufferLength: Math.round(960 * audioContext.sampleRate / 24000),
    decoderSampleRate: 24000,
    outputBufferSampleRate: audioContext.sampleRate,
    resampleQuality: 0,
  });
  decoder.onmessage = event => {
    const pcm = event.data?.[0] as Float32Array | undefined;
    if (!pcm?.length || !audioContext) return;
    observeAssistantPcm(pcm);
    const buffer = audioContext.createBuffer(1, pcm.length, audioContext.sampleRate);
    buffer.copyToChannel(pcm, 0);
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);
    const floor = audioContext.currentTime + 0.015;
    playbackAt = Math.max(playbackAt, floor);
    source.start(playbackAt);
    playbackAt += buffer.duration;
    queueEl.textContent = `${Math.max(0, Math.round((playbackAt - audioContext.currentTime) * 1000))} ms`;
  };
}

async function startRecorder(): Promise<void> {
  if (!audioContext) throw new Error("audio context missing");
  recorder = new Recorder({
    encoderPath: "/assets/encoderWorker.min.js",
    bufferLength: Math.round(960 * audioContext.sampleRate / 24000),
    encoderFrameSize: 20,
    encoderSampleRate: 24000,
    maxFramesPerPage: 2,
    numberOfChannels: 1,
    recordingGain: 1,
    resampleQuality: 3,
    encoderComplexity: 0,
    encoderApplication: 2049,
    streamPages: true,
    leaveStreamOpen: false,
    mediaTrackConstraints: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  recorder.ondataavailable = (data: Uint8Array) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(data);
  };
  recorder.onerror = (error: unknown) => setStatus(`mic error: ${String(error)}`);
  micStream = await recorder.initStream();
  startVad(micStream!);
  recorder.start();
  micEl.textContent = "live";
}

async function disconnect(): Promise<void> {
  stopVad();
  if (recorder) {
    try { recorder.stop(); } catch {}
    recorder = null;
  }
  micStream?.getTracks().forEach(track => track.stop());
  micStream = null;
  decoder?.terminate();
  decoder = null;
  ws?.close();
  ws = null;
  if (audioContext) {
    await audioContext.close().catch(() => {});
    audioContext = null;
  }
  playbackAt = 0;
  if (assistantSpeechActive) {
    sendEvent("assistant.speech.stopped", { reason: "disconnect", observed_at: "playback" });
  }
  assistantSpeechActive = false;
  assistantBelowSince = 0;
  micEl.textContent = "off";
  codecEl.textContent = "—";
  queueEl.textContent = "0 ms";
  connectButton.disabled = false;
  disconnectButton.disabled = true;
  floorEl.textContent = "idle";
  setStatus("disconnected");
}

async function connect(): Promise<void> {
  connectButton.disabled = true;
  setStatus("connecting");
  traceId = crypto.randomUUID();
  const url = gatewayInput.value.trim();
  localStorage.setItem("z0live.gateway", url);

  audioContext = new AudioContext({ latencyHint: "interactive" });
  await audioContext.resume();
  initDecoder();

  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  ws = socket;

  socket.onmessage = async event => {
    if (typeof event.data !== "string") {
      const bytes = new Uint8Array(event.data as ArrayBuffer);
      decoder?.postMessage({ command: "decode", pages: bytes }, [bytes.buffer]);
      return;
    }
    const message = JSON.parse(event.data);
    if (message.type === "hello") {
      const caps = message.actor_capabilities || {};
      codecEl.textContent = `${caps.input_codec || "?"}/${caps.input_sample_rate_hz || "?"}`;
      if (caps.input_codec !== "ogg-opus" || Number(caps.input_sample_rate_hz) !== 24000) {
        setStatus("this web client currently requires Ogg/Opus 24k input");
        socket.close();
        return;
      }
      try {
        await startRecorder();
        setStatus(`connected · ${message.actor_id}`);
        disconnectButton.disabled = false;
      } catch (error) {
        setStatus(`microphone failed: ${String(error)}`);
        await disconnect();
      }
      return;
    }
    if (message.type === "event") {
      const ev = message.event || {};
      if (ev.kind === "assistant.speech.started") floorEl.textContent = "assistant";
      if (ev.kind === "assistant.speech.stopped" || ev.kind === "playback.silence") {
        floorEl.textContent = speechActive ? "you" : "idle";
      }
      if (ev.kind === "assistant.transcript.delta") {
        transcriptEl.textContent += String(ev.payload?.text || "");
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
      }
      if (ev.kind === "actor.error") {
        setStatus(`actor error: ${String(ev.payload?.error || "unknown")}`);
      }
    }
  };

  socket.onerror = () => setStatus("gateway connection error");
  socket.onclose = () => {
    if (ws === socket) void disconnect();
  };
}

connectButton.addEventListener("click", () => void connect());
disconnectButton.addEventListener("click", () => void disconnect());
window.addEventListener("beforeunload", () => {
  try { recorder?.stop(); } catch {}
  ws?.close();
});

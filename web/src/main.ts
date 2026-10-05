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
let transcriberCaps: Record<string, unknown> | null = null;
let controlSource: MediaStreamAudioSourceNode | null = null;
let controlProcessor: ScriptProcessorNode | null = null;
let controlMute: GainNode | null = null;

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

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function downsampleMono(input: Float32Array, inputRate: number, outputRate: number): Float32Array {
  if (outputRate <= 0 || inputRate < outputRate) return new Float32Array(0);
  if (inputRate === outputRate) return new Float32Array(input);
  const ratio = inputRate / outputRate;
  const outputLength = Math.max(1, Math.floor(input.length / ratio));
  const output = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    output[i] = sum / Math.max(1, end - start);
  }
  return output;
}

function pcm16Bytes(input: Float32Array): Uint8Array {
  const bytes = new Uint8Array(input.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < input.length; i++) {
    const sample = Math.max(-1, Math.min(1, input[i]));
    const value = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
    view.setInt16(i * 2, value, true);
  }
  return bytes;
}

function startControlTap(stream: MediaStream): void {
  if (!audioContext || !transcriberCaps || !ws) return;
  const codec = String(transcriberCaps.input_codec || "");
  const sampleRate = Number(transcriberCaps.input_sample_rate_hz || 0);
  const channels = Number(transcriberCaps.channels || 1);
  if (codec !== "pcm16" || sampleRate !== 16000 || channels !== 1) {
    setStatus(`control lane unsupported: ${codec}/${sampleRate}/${channels}`);
    return;
  }

  controlSource = audioContext.createMediaStreamSource(stream);
  controlProcessor = audioContext.createScriptProcessor(4096, 1, 1);
  controlMute = audioContext.createGain();
  controlMute.gain.value = 0;

  controlProcessor.onaudioprocess = event => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !audioContext) return;
    const mono = event.inputBuffer.getChannelData(0);
    const pcm = downsampleMono(mono, audioContext.sampleRate, sampleRate);
    if (!pcm.length) return;
    const bytes = pcm16Bytes(pcm);
    ws.send(JSON.stringify({
      type: "transcriber.audio",
      audio: {
        codec: "pcm16",
        sample_rate_hz: sampleRate,
        channels: 1,
        duration_ms: pcm.length * 1000 / sampleRate,
        data_base64: bytesToBase64(bytes),
      },
    }));
  };

  controlSource.connect(controlProcessor);
  controlProcessor.connect(controlMute);
  controlMute.connect(audioContext.destination);
}

function stopControlTap(): void {
  if (controlProcessor) controlProcessor.onaudioprocess = null;
  controlSource?.disconnect();
  controlProcessor?.disconnect();
  controlMute?.disconnect();
  controlSource = null;
  controlProcessor = null;
  controlMute = null;
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

function observeAssistantPcm(pcm: Float32Array, playbackDelayMs: number): void {
  let energy = 0;
  for (const sample of pcm) energy += sample * sample;
  const rms = Math.sqrt(energy / Math.max(1, pcm.length));
  const now = performance.now();

  if (!assistantSpeechActive && rms >= 0.012) {
    assistantSpeechActive = true;
    assistantBelowSince = 0;
    floorEl.textContent = speechActive ? "overlap" : "assistant";
    sendEvent("assistant.speech.started", {
      rms,
      observed_at: "decoded_pcm",
      playback_delay_ms: playbackDelayMs,
    });
  } else if (assistantSpeechActive) {
    if (rms < 0.006) {
      if (!assistantBelowSince) assistantBelowSince = now;
      if (now - assistantBelowSince >= 240) {
        assistantSpeechActive = false;
        assistantBelowSince = 0;
        floorEl.textContent = speechActive ? "you" : "idle";
        sendEvent("assistant.speech.stopped", {
          rms,
          observed_at: "decoded_pcm",
          playback_delay_ms: playbackDelayMs,
        });
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
    const floor = audioContext.currentTime + 0.015;
    const scheduledStart = Math.max(playbackAt, floor);
    const playbackDelayMs = Math.max(
      0,
      (scheduledStart - audioContext.currentTime) * 1000,
    );
    observeAssistantPcm(pcm, playbackDelayMs);
    const buffer = audioContext.createBuffer(1, pcm.length, audioContext.sampleRate);
    const copy = new Float32Array(pcm.length);
    copy.set(pcm);
    buffer.copyToChannel(copy, 0);
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);
    source.start(scheduledStart);
    playbackAt = scheduledStart + buffer.duration;
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
  startControlTap(micStream!);
  recorder.start();
  micEl.textContent = "live";
}

async function disconnect(): Promise<void> {
  stopControlTap();
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
  transcriberCaps = null;
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
      transcriberCaps = message.transcriber_capabilities || null;
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

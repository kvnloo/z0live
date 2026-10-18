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
const commandForm = $<HTMLFormElement>("command-form");
const commandInput = $<HTMLInputElement>("command-text");
const commandButton = $<HTMLButtonElement>("command-send");
const commandStatus = $<HTMLParagraphElement>("command-status");

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
let actorInputCodec = "";
let actorOutputCodec = "";
let pendingCommandId: string | null = null;
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
  if (!audioContext || !ws) return;
  const codec = String(transcriberCaps?.input_codec || "");
  const sampleRate = Number(transcriberCaps?.input_sample_rate_hz || 0);
  const channels = Number(transcriberCaps?.channels || 1);
  const wantsControl = codec === "pcm16" && sampleRate === 16000 && channels === 1;
  const wantsActorPcm = actorInputCodec === "pcm16";
  if (!wantsActorPcm && !wantsControl) return;
  if (transcriberCaps && !wantsControl) {
    commandStatus.textContent = `Separate ASR lane unavailable: ${codec}/${sampleRate}/${channels}`;
  }

  // Single microphone graph feeds the selected actor and optional control STT.
  // A ScriptProcessor is widely available on Android browsers; migrate to a
  // worklet after physical-device latency/underrun measurements.
  controlSource = audioContext.createMediaStreamSource(stream);
  controlProcessor = audioContext.createScriptProcessor(2048, 1, 1);
  controlMute = audioContext.createGain();
  controlMute.gain.value = 0;

  controlProcessor.onaudioprocess = event => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !audioContext) return;
    const mono = event.inputBuffer.getChannelData(0);
    if (wantsActorPcm) {
      const pcm = downsampleMono(mono, audioContext.sampleRate, 24000);
      if (pcm.length) ws.send(pcm16Bytes(pcm));
    }
    if (wantsControl) {
      const pcm = downsampleMono(mono, audioContext.sampleRate, 16000);
      if (!pcm.length) return;
      ws.send(JSON.stringify({
        type: "transcriber.audio",
        audio: {
          codec: "pcm16",
          sample_rate_hz: 16000,
          channels: 1,
          duration_ms: pcm.length * 1000 / 16000,
          data_base64: bytesToBase64(pcm16Bytes(pcm)),
        },
      }));
    }
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

function playPcm(pcm: Float32Array, inputRate: number): void {
  if (!audioContext || !pcm.length) return;
  const floor = audioContext.currentTime + 0.015;
  const scheduledStart = Math.max(playbackAt, floor);
  const playbackDelayMs = Math.max(0, (scheduledStart - audioContext.currentTime) * 1000);
  observeAssistantPcm(pcm, playbackDelayMs);
  const buffer = audioContext.createBuffer(1, pcm.length, inputRate);
  buffer.copyToChannel(new Float32Array(pcm), 0);
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioContext.destination);
  source.start(scheduledStart);
  playbackAt = scheduledStart + buffer.duration;
  queueEl.textContent = `${Math.max(0, Math.round((playbackAt - audioContext.currentTime) * 1000))} ms`;
}

function pcm16FromBytes(bytes: Uint8Array): Float32Array {
  if (bytes.byteLength % 2 !== 0) throw new Error("invalid PCM16 byte count");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const samples = new Float32Array(bytes.byteLength / 2);
  for (let i = 0; i < samples.length; i++) {
    samples[i] = view.getInt16(i * 2, true) / 32768;
  }
  return samples;
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
    if (pcm?.length && audioContext) playPcm(pcm, audioContext.sampleRate);
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

async function startPcmMicrophone(): Promise<void> {
  if (!audioContext) throw new Error("audio context missing");
  micStream = await navigator.mediaDevices.getUserMedia({ audio: {
    channelCount: 1, echoCancellation: true, noiseSuppression: true,
    autoGainControl: true,
  }});
  startVad(micStream);
  startControlTap(micStream);
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
  actorInputCodec = "";
  actorOutputCodec = "";
  pendingCommandId = null;
  commandButton.disabled = true;
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
  // A browser can reach 127.0.0.1 in its own Android network namespace.
  // Refuse off-device microphone streaming until a separately authenticated
  // and TLS-secured transport exists.
  let parsed: URL;
  try {
    parsed = new URL(url);
    if (parsed.protocol !== "ws:" ||
        !["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname)) {
      throw new Error("not a local WebSocket");
    }
  } catch {
    setStatus("Hybrid requires a loopback ws:// gateway in this phone");
    connectButton.disabled = false;
    return;
  }
  localStorage.setItem("z0live.gateway", parsed.href);

  audioContext = new AudioContext({ latencyHint: "interactive" });
  await audioContext.resume();

  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  ws = socket;

  socket.onmessage = async event => {
    if (typeof event.data !== "string") {
      const bytes = new Uint8Array(event.data as ArrayBuffer);
      if (actorOutputCodec === "pcm16") {
        try { playPcm(pcm16FromBytes(bytes), 24000); }
        catch (error) { setStatus(`invalid PCM audio: ${String(error)}`); }
      } else if (actorOutputCodec === "ogg-opus") {
        decoder?.postMessage({ command: "decode", pages: bytes }, [bytes.buffer]);
      }
      return;
    }
    const message = JSON.parse(event.data);
    if (message.type === "hello") {
      const caps = message.actor_capabilities || {};
      transcriberCaps = message.transcriber_capabilities || null;
      actorInputCodec = String(caps.input_codec || "");
      actorOutputCodec = String(caps.output_codec || "");
      codecEl.textContent = `${actorInputCodec}/${caps.input_sample_rate_hz || "?"} → ${actorOutputCodec}`;
      const supported = ["pcm16", "ogg-opus"];
      if (!supported.includes(actorInputCodec) || !supported.includes(actorOutputCodec) ||
          Number(caps.input_sample_rate_hz) !== 24000 ||
          Number(caps.output_sample_rate_hz) !== 24000) {
        setStatus("unsupported voice codec (requires PCM16 or Ogg/Opus at 24 kHz)");
        socket.close();
        return;
      }
      try {
        if (actorOutputCodec === "ogg-opus") initDecoder();
        if (actorInputCodec === "ogg-opus") await startRecorder();
        else await startPcmMicrophone();
        commandButton.disabled = !(message.harness_capabilities?.submit === true);
        commandStatus.textContent = commandButton.disabled
          ? "No OMP/Hermes harness connected; voice audio only."
          : "OMP/Hermes ready. Actions still require harness/tool approvals.";
        setStatus(`connected · ${message.actor_id}`);
        disconnectButton.disabled = false;
      } catch (error) {
        setStatus(`microphone failed: ${String(error)}`);
        await disconnect();
      }
      return;
    }
    if (message.type === "harness.result") {
      if (String(message.command_id) === pendingCommandId) {
        pendingCommandId = null;
        commandButton.disabled = false;
        const result = message.result || {};
        commandStatus.textContent = result.ok
          ? "Command accepted by harness; confirm result in OMP/Hermes."
          : `Harness refused: ${String(result.error || "unknown")}`;
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

commandForm.addEventListener("submit", event => {
  event.preventDefault();
  if (!ws || ws.readyState !== WebSocket.OPEN || commandButton.disabled || pendingCommandId) return;
  const text = commandInput.value.trim();
  if (!text) return;
  pendingCommandId = crypto.randomUUID();
  commandButton.disabled = true;
  commandStatus.textContent = "Submitted; checking harness receipt…";
  ws.send(JSON.stringify({
    type: "harness.command",
    command: {
      kind: "submit", command_id: pendingCommandId, trace_id: traceId, text,
      payload: { source: "hybrid_user_explicit_submit" },
    },
  }));
  commandInput.value = "";
});

connectButton.addEventListener("click", () => void connect());
disconnectButton.addEventListener("click", () => void disconnect());
window.addEventListener("beforeunload", () => {
  try { recorder?.stop(); } catch {}
  ws?.close();
});

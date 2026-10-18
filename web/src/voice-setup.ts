import "./voice-setup.css";

type Preset = "s25" | "other" | "desktop";
type RouteId =
  | "hybrid" | "chatgpt" | "android_assist"
  | "realtime" | "offline_asr" | "moshi" | "wispr";

type Route = {
  id: RouteId;
  name: string;
  detail: string;
  badge?: string;
  guidance: string;
  href?: string;
  action?: string;
};

const routes: Route[] = [
  {
    id: "hybrid", name: "Hybrid · phone + Tern", badge: "recommended",
    detail: "Phone microphone / launcher, Hermes or OMP in Tern, verified actions over Wi-Fi ADB.",
    guidance: "Keep your existing ADB pairing. Install Hermes phone-harness on the Tern host and keep script approvals on. Start a z0live host session with an admitted VoicePlan; phone-to-host streaming needs an explicitly secured transport. Selecting this route does not establish that transport.",
    href: "https://github.com/ShawnPana/phone-harness/tree/main/integrations/hermes",
    action: "Hermes phone plugin",
  },
  {
    id: "chatgpt", name: "ChatGPT Voice · official app",
    detail: "Open the installed app, sign in normally, and enter Voice there.",
    guidance: "Use Lawnchair's Assistants → Open ChatGPT app, or install ChatGPT first. Your ChatGPT subscription/login is not an OpenAI developer API credential and cannot silently authenticate z0live Realtime.",
    href: "https://play.google.com/store/apps/details?id=com.openai.chatgpt",
    action: "ChatGPT on Google Play",
  },
  {
    id: "android_assist", name: "Google / Gemini · Android assistant",
    detail: "Let Android launch the assistant selected in your device settings.",
    guidance: "Use Lawnchair's Assistants → Open default assistant or change it in Android Default apps settings. No third-party voice transcript or function-calling access is implied.",
  },
  {
    id: "realtime", name: "OpenAI Realtime · developer API",
    detail: "Streaming speech and tool events through your configured z0live host.",
    guidance: "Configure OpenAI Realtime credentials on the trusted host and request a VoicePlan from z0intelligence. Do not put a permanent API key in the phone APK, browser storage, or URL. A consumer ChatGPT login does not provision this API.",
    href: "/", action: "Open z0live session",
  },
  {
    id: "offline_asr", name: "On-device speech · Android / sherpa-onnx",
    badge: "local STT",
    detail: "Offline recognition first; full-duplex voice and action execution are separate pieces.",
    guidance: "Test the phone's on-device SpeechRecognizer and then a licensed sherpa-onnx Qualcomm QNN streaming-ASR build. QNN targets the NPU, not the GPU. Measure p50/p95 and battery on the actual device before enabling continuous listening. Selecting this route does not install a model or grant mic permission.",
    href: "https://k2-fsa.github.io/sherpa/onnx/android/apk-qnn-asr-streaming.html",
    action: "Inspect QNN demo builds",
  },
  {
    id: "moshi", name: "Moshi / PersonaPlex · local PC",
    detail: "Run the full-duplex model on a validated GPU host, stream audio from the phone.",
    guidance: "Use the existing z0live PersonaPlex host adapter and test on the RTX 3080 Ti first. Mobile GPU execution is experimental: phone SoC and RAM specifications alone are not evidence that a compatible Android full-duplex runtime exists. Never download model weights without an explicit storage and license check.",
    href: "https://github.com/kyutai-labs/moshi",
    action: "Moshi sources",
  },
  {
    id: "wispr", name: "Wispr Flow · dictation app",
    detail: "Cloud-assisted Android speech-to-text; not a local Moshi/Whisper model.",
    guidance: "Install and sign in to Wispr Flow in its own Android app. Its floating dictation bubble needs permissions explained by Wispr; it requires an internet connection and does not supply z0live's full-duplex agent runtime.",
    href: "https://play.google.com/store/apps/details?id=com.wispr.flowapp",
    action: "Wispr Flow on Google Play",
  },
];

const $ = <T extends HTMLElement>(id: string): T => {
  const found = document.getElementById(id);
  if (!found) throw new Error("Missing voice setup element: " + id);
  return found as T;
};
const presetEl = $<HTMLSelectElement>("preset");
const routesEl = $<HTMLDivElement>("routes");
const guidanceEl = $<HTMLParagraphElement>("guidance");
const inventoryEl = $<HTMLParagraphElement>("inventory");
const receiptEl = $<HTMLElement>("receipt");
const handoffEl = $<HTMLAnchorElement>("handoff");
const copyEl = $<HTMLButtonElement>("copy");
const copyStatusEl = $<HTMLParagraphElement>("copy-status");
const probeEl = $<HTMLButtonElement>("probe");

let selected: RouteId = "hybrid";
let inspected: string | null = null;

function supportText(route: Route, preset: Preset): string {
  if (route.id === "hybrid" && preset === "s25") return "Matches the known Wi-Fi ADB architecture; transport still needs testing.";
  if (route.id === "offline_asr") {
    return preset === "s25"
      ? "Snapdragon 8 Elite is a plausible QNN test target, not a certified model benchmark."
      : "Availability depends on an installed on-device recognizer or compatible QNN model.";
  }
  if (route.id === "moshi" && preset !== "desktop")
    return "Prefer host GPU; on-phone model compatibility not established.";
  if (route.id === "wispr") return "Android 13+ and internet required.";
  return "";
}

function render(): void {
  const preset = presetEl.value as Preset;
  routesEl.replaceChildren();
  for (const route of routes) {
    const label = document.createElement("label");
    label.className = "route";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "voice-route";
    input.value = route.id;
    input.checked = route.id === selected;
    input.addEventListener("change", () => {
      selected = route.id;
      render();
    });
    const body = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = route.name;
    body.append(title);
    if (route.badge) {
      const badge = document.createElement("em");
      badge.textContent = route.badge;
      body.append(badge);
    }
    const detail = document.createElement("small");
    detail.textContent = route.detail;
    body.append(detail);
    const support = supportText(route, preset);
    if (support) {
      const extra = document.createElement("small");
      extra.textContent = support;
      body.append(extra);
    }
    label.append(input, body);
    routesEl.append(label);
  }
  const route = routes.find((r) => r.id === selected);
  if (!route) throw new Error("Invalid route selection");
  guidanceEl.textContent = route.guidance;
  handoffEl.hidden = !route.href;
  if (route.href) {
    handoffEl.href = route.href;
    handoffEl.textContent = route.action || "Open";
    if (route.href.startsWith("https://")) {
      handoffEl.target = "_blank";
      handoffEl.rel = "noopener noreferrer";
    } else {
      handoffEl.removeAttribute("target");
      handoffEl.removeAttribute("rel");
    }
  }
  const receipt = {
    schema: "z0live.voice_preference.v1",
    route: selected,
    manually_selected_device: preset,
    hardware_probe: inspected || "not performed",
    status: "preference only; not an admitted VoicePlan",
  };
  receiptEl.textContent = JSON.stringify(receipt, null, 2);
  copyStatusEl.textContent = "";
}

presetEl.addEventListener("change", () => {
  inspected = null;
  inventoryEl.textContent = "Manual profile: " + presetEl.selectedOptions[0].text + ". No hardware measurement.";
  render();
});

probeEl.addEventListener("click", () => {
  // Browser hints are unreliable; never infer a model's measured GPU/NPU performance.
  const isAndroid = /Android/i.test(navigator.userAgent);
  const cores = navigator.hardwareConcurrency || "unknown";
  const mem = (navigator as Navigator & {deviceMemory?: number}).deviceMemory;
  let renderer = "unavailable";
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl");
  if (gl) {
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    renderer = String(gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
    gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
  inspected = "browser=" + (isAndroid ? "Android" : "not Android") +
    "; logical_cores=" + cores + "; deviceMemoryGB=" + (mem || "unavailable") +
    "; webgl_renderer=" + renderer;
  inventoryEl.textContent = inspected + ". These are browser hints only.";
  render();
});

copyEl.addEventListener("click", async () => {
  const preference = receiptEl.textContent || "";
  try {
    await navigator.clipboard.writeText(preference);
    copyStatusEl.textContent = "Preference copied. This does not start a model or grant access.";
  } catch {
    copyStatusEl.textContent = "Clipboard blocked. Expand Selection details to copy manually.";
  }
});

render();

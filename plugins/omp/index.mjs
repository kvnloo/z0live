import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve, delimiter } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const Z0LIVE_ROOT = resolve(PLUGIN_DIR, "../..");
const STATUS_KEY = "z0live";
const DEFAULT_GATEWAY = "127.0.0.1:8765";

const state = {
  phase: "off",
  process: null,
  bridge: null,
  tempDir: null,
  planPath: null,
  mainCtx: null,
  mainPi: null,
  traceId: null,
  monitor: null,
  stderrTail: "",
};

function pythonBin() {
  return process.env.PYTHON || process.env.PYTHON_BIN || "python3";
}

function withPythonPath(env, root) {
  const src = join(root, "src");
  return {
    ...env,
    PYTHONPATH: env.PYTHONPATH ? `${src}${delimiter}${env.PYTHONPATH}` : src,
  };
}

function z0intEnv() {
  const root = process.env.Z0INTELLIGENCE_ROOT;
  return root ? withPythonPath(process.env, resolve(root)) : { ...process.env };
}

function z0liveEnv() {
  return withPythonPath(process.env, Z0LIVE_ROOT);
}

function z0liveHome() {
  return resolve(process.env.Z0LIVE_HOME || join(homedir(), ".z0live"));
}

function sessionStatePath() {
  return join(z0liveHome(), "session.json");
}

function setStatus(ctx, text) {
  if (!ctx?.ui) return;
  ctx.ui.setStatus(STATUS_KEY, text || undefined);
}

function notify(ctx, text, kind = "info") {
  if (ctx?.ui) ctx.ui.notify(text, kind);
}

function appendTail(current, chunk, max = 4000) {
  const next = current + String(chunk);
  return next.length <= max ? next : next.slice(next.length - max);
}

function runCaptured(command, args, options = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", chunk => {
      stdout = appendTail(stdout, chunk, 16000);
    });
    child.stderr?.on("data", chunk => {
      stderr = appendTail(stderr, chunk, 16000);
    });
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      resolvePromise({ code: code ?? -1, signal, stdout, stderr });
    });
  });
}

export function extractTextContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map(item => {
      if (!item || typeof item !== "object") return "";
      if (item.type === "text" && typeof item.text === "string") return item.text;
      return "";
    })
    .filter(Boolean)
    .join("\n")
    .trim();
}

export function extractAssistantText(message) {
  if (!message || typeof message !== "object") return "";
  return extractTextContent(message.content);
}

export function verifiedFromDetails(details) {
  if (!details || typeof details !== "object") return false;
  if (details.verified_success === true || details.verified === true) return true;
  if (details.result && typeof details.result === "object") {
    return details.result.verified_success === true || details.result.verified === true;
  }
  return false;
}

function event(kind, payload = {}, extras = {}) {
  return {
    event_id: randomUUID(),
    kind,
    source: "omp",
    at_ms: Date.now(),
    payload,
    ...extras,
  };
}

class HarnessBridge {
  constructor(onCommand) {
    this.onCommand = onCommand;
    this.server = null;
    this.sockets = new Set();
    this.port = null;
    this.buffers = new WeakMap();
  }

  async start() {
    this.server = createServer(socket => {
      socket.setNoDelay(true);
      this.sockets.add(socket);
      this.buffers.set(socket, "");
      socket.on("close", () => this.sockets.delete(socket));
      socket.on("error", () => this.sockets.delete(socket));
      socket.on("data", chunk => this.#onData(socket, chunk));
      this.#write(socket, {
        type: "hello",
        capabilities: {
          submit: true,
          steer: true,
          redirect: true,
          cancel: true,
          approvals: false,
          progress_events: true,
          verified_results: true,
        },
      });
    });
    await new Promise((resolvePromise, reject) => {
      const onError = error => {
        this.server?.off("listening", onListening);
        reject(error);
      };
      const onListening = () => {
        this.server?.off("error", onError);
        resolvePromise();
      };
      this.server.once("error", onError);
      this.server.once("listening", onListening);
      this.server.listen(0, "127.0.0.1");
    });
    const address = this.server.address();
    if (!address || typeof address === "string") throw new Error("failed to bind OMP harness bridge");
    this.port = address.port;
    return this.port;
  }

  async #onData(socket, chunk) {
    let buffer = (this.buffers.get(socket) || "") + chunk.toString("utf8");
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) break;
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      let raw;
      try {
        raw = JSON.parse(line);
      } catch (error) {
        this.#write(socket, { type: "error", error: `invalid JSON: ${error}` });
        continue;
      }
      if (raw.type !== "command" || !raw.command) continue;
      const command = raw.command;
      try {
        const result = await this.onCommand(command);
        this.#write(socket, {
          type: "result",
          command_id: command.command_id,
          result: result || { ok: true },
        });
      } catch (error) {
        this.#write(socket, {
          type: "result",
          command_id: command.command_id,
          result: { ok: false, error: String(error?.message || error) },
        });
      }
    }
    this.buffers.set(socket, buffer);
  }

  #write(socket, value) {
    if (!socket.destroyed) socket.write(JSON.stringify(value) + "\n");
  }

  emit(timelineEvent) {
    const line = JSON.stringify({ type: "event", event: timelineEvent }) + "\n";
    for (const socket of this.sockets) {
      if (!socket.destroyed) socket.write(line);
    }
  }

  async close() {
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
    if (!this.server) return;
    const server = this.server;
    this.server = null;
    await new Promise(resolvePromise => server.close(() => resolvePromise()));
  }
}

export async function executeHarnessCommand(command, pi, ctx) {
  if (!ctx || ctx.agent?.kind !== "main") throw new Error("OMP main session is unavailable");
  const text = typeof command.text === "string" ? command.text.trim() : "";
  state.traceId = command.trace_id || state.traceId;

  switch (command.kind) {
    case "submit":
      if (!text) throw new Error("submit requires text");
      if (ctx.isIdle()) {
        pi.sendUserMessage(text, { attribution: "user" });
      } else {
        pi.sendUserMessage(text, { deliverAs: "followUp", attribution: "user" });
      }
      return { ok: true, accepted: "submit" };
    case "steer":
      if (!text) throw new Error("steer requires text");
      pi.sendUserMessage(text, { deliverAs: "steer", attribution: "user" });
      return { ok: true, accepted: "steer" };
    case "redirect":
      if (!text) throw new Error("redirect requires text");
      ctx.abort();
      pi.sendUserMessage(text, { attribution: "user" });
      return { ok: true, accepted: "redirect" };
    case "cancel":
      ctx.abort();
      return { ok: true, accepted: "cancel" };
    case "approve":
      return { ok: false, unsupported: "approval resolution is not exposed by the extension API" };
    default:
      throw new Error(`unsupported harness command: ${command.kind}`);
  }
}

async function stopBrainstorm(ctx, reason = "explicit_exit") {
  const processHandle = state.process;
  state.process = null;
  state.phase = "stopping";
  setStatus(ctx || state.mainCtx, "brainstorm: stopping");

  if (processHandle && processHandle.exitCode === null && !processHandle.killed) {
    processHandle.kill("SIGTERM");
    await Promise.race([
      new Promise(resolvePromise => processHandle.once("exit", resolvePromise)),
      new Promise(resolvePromise => setTimeout(resolvePromise, 4000)),
    ]);
    if (processHandle.exitCode === null) processHandle.kill("SIGKILL");
  }

  if (state.bridge) {
    await state.bridge.close();
    state.bridge = null;
  }
  if (state.monitor && state.mainCtx) {
    state.mainCtx.clearTimer(state.monitor);
    state.monitor = null;
  }
  if (state.tempDir) {
    rmSync(state.tempDir, { recursive: true, force: true });
    state.tempDir = null;
    state.planPath = null;
  }

  state.phase = "off";
  state.traceId = null;
  setStatus(ctx || state.mainCtx, undefined);
  if (reason !== "session_shutdown") notify(ctx || state.mainCtx, `Brainstorm Mode stopped (${reason})`, "info");
}

function monitorService(ctx) {
  if (state.monitor) ctx.clearTimer(state.monitor);
  state.monitor = ctx.setInterval(() => {
    const child = state.process;
    if (!child) return;
    if (child.exitCode !== null) {
      const tail = state.stderrTail.trim();
      void stopBrainstorm(ctx, `z0live exit ${child.exitCode}`);
      if (tail) notify(ctx, tail.slice(-600), "error");
      return;
    }
    try {
      const raw = JSON.parse(readFileSync(sessionStatePath(), "utf8"));
      if (raw.phase === "ready") {
        state.phase = "ready";
        setStatus(ctx, "brainstorm: ready");
      } else {
        state.phase = String(raw.phase || "warming");
        setStatus(ctx, `brainstorm: ${state.phase}`);
      }
    } catch {
      if (state.phase !== "ready") setStatus(ctx, "brainstorm: warming");
    }
  }, 500);
}

async function startBrainstorm(pi, ctx) {
  if (ctx.agent?.kind !== "main") throw new Error("Brainstorm Mode can only be started from the main OMP session");
  if (state.process && state.process.exitCode === null) {
    notify(ctx, `Brainstorm Mode is already ${state.phase}`, "info");
    return;
  }

  state.mainCtx = ctx;
  state.mainPi = pi;
  state.phase = "planning";
  setStatus(ctx, "brainstorm: planning");

  const bridge = new HarnessBridge(command => executeHarnessCommand(command, pi, state.mainCtx));
  const bridgePort = await bridge.start();
  state.bridge = bridge;

  const tempDir = mkdtempSync(join(tmpdir(), "z0live-omp-"));
  const planPath = join(tempDir, "voice-plan.json");
  state.tempDir = tempDir;
  state.planPath = planPath;

  const planResult = await runCaptured(
    pythonBin(),
    ["-m", "z0int.voice_plan", "brainstorm", "--harness", "omp", "--pretty", "--output", planPath],
    { env: z0intEnv() },
  );

  if (planResult.code !== 0) {
    let detail = planResult.stderr.trim();
    if (existsSync(planPath)) {
      try {
        const plan = JSON.parse(readFileSync(planPath, "utf8"));
        const admission = plan.admission || {};
        detail = admission.reclaim_needed_mb
          ? `GPU busy: reclaim ${admission.reclaim_needed_mb} MB for Brainstorm Mode`
          : `Brainstorm admission refused: ${admission.reason || admission.status || "unknown"}`;
      } catch {}
    }
    await stopBrainstorm(ctx, "admission_refused");
    throw new Error(detail || "z0intelligence refused Brainstorm admission");
  }

  state.phase = "warming";
  setStatus(ctx, "brainstorm: warming");

  const gateway = process.env.Z0LIVE_GATEWAY || DEFAULT_GATEWAY;
  const args = [
    "-m",
    "z0live",
    "serve",
    "--plan",
    planPath,
    "--harness",
    `127.0.0.1:${bridgePort}`,
    "--listen",
    gateway,
  ];
  const child = spawn(pythonBin(), args, {
    cwd: Z0LIVE_ROOT,
    env: z0liveEnv(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  state.process = child;
  state.stderrTail = "";
  child.stdout?.on("data", chunk => {
    pi.logger.debug?.(`[z0live] ${String(chunk).trim()}`);
  });
  child.stderr?.on("data", chunk => {
    state.stderrTail = appendTail(state.stderrTail, chunk);
    pi.logger.debug?.(`[z0live] ${String(chunk).trim()}`);
  });
  child.once("error", error => {
    state.stderrTail = appendTail(state.stderrTail, String(error));
  });
  child.once("exit", code => {
    if (state.process === child) {
      state.phase = "off";
      state.process = null;
      setStatus(state.mainCtx, undefined);
      if (code && code !== 0) notify(state.mainCtx, `z0live exited with code ${code}`, "error");
    }
  });

  monitorService(ctx);
  notify(ctx, "Brainstorm Mode is warming; PersonaPlex is session-scoped and will unload on exit/idle.", "info");
}

function emit(kind, payload = {}, extras = {}) {
  state.bridge?.emit(event(kind, payload, {
    trace_id: state.traceId || undefined,
    ...extras,
  }));
}

export default function z0liveOmpPlugin(pi) {
  pi.setLabel("z0live Brainstorm");

  pi.registerCommand("brainstorm", {
    description: "Start/stop/status for on-demand z0live Brainstorm Mode",
    handler: async (args, ctx) => {
      const action = args.trim().toLowerCase() || (state.process ? "status" : "on");
      if (action === "on" || action === "start") {
        try {
          await startBrainstorm(pi, ctx);
        } catch (error) {
          setStatus(ctx, undefined);
          notify(ctx, String(error?.message || error), "error");
        }
        return;
      }
      if (action === "off" || action === "stop") {
        await stopBrainstorm(ctx);
        return;
      }
      if (action === "restart") {
        await stopBrainstorm(ctx, "restart");
        await startBrainstorm(pi, ctx);
        return;
      }
      if (action === "status") {
        const pid = state.process?.pid;
        notify(ctx, `Brainstorm Mode: ${state.phase}${pid ? ` (z0live pid ${pid})` : ""}`, "info");
        return;
      }
      notify(ctx, "Usage: /brainstorm [on|off|restart|status]", "warning");
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.agent?.kind === "main") {
      state.mainCtx = ctx;
      state.mainPi = pi;
      if (state.process && state.process.exitCode === null) monitorService(ctx);
    }
  });

  pi.on("turn_start", async eventValue => {
    emit("harness.progress", {
      text: `OMP turn ${eventValue.turnIndex} started`,
      phase: "turn_start",
    });
  });

  pi.on("tool_result", async eventValue => {
    const text = extractTextContent(eventValue.content);
    if (eventValue.isError) {
      emit("harness.error", {
        text: text || `${eventValue.toolName} failed`,
        tool: eventValue.toolName,
        tool_call_id: eventValue.toolCallId,
      });
      return;
    }
    emit("harness.progress", {
      text: text || `${eventValue.toolName} completed`,
      tool: eventValue.toolName,
      tool_call_id: eventValue.toolCallId,
      verified: verifiedFromDetails(eventValue.details),
    });
  });

  pi.on("tool_approval_requested", async eventValue => {
    emit("harness.approval", {
      text: eventValue.reason || `Approval required for ${eventValue.toolName}`,
      tool: eventValue.toolName,
      tool_call_id: eventValue.toolCallId,
      approval_mode: eventValue.approvalMode,
    });
  });

  pi.on("tool_approval_resolved", async eventValue => {
    emit("harness.progress", {
      text: `Approval ${eventValue.approved ? "granted" : "denied"} for ${eventValue.toolName}`,
      tool: eventValue.toolName,
      tool_call_id: eventValue.toolCallId,
      approved: eventValue.approved,
    });
  });

  pi.on("agent_end", async eventValue => {
    if (eventValue.willContinue) return;
    const last = Array.isArray(eventValue.messages) ? eventValue.messages.at(-1) : undefined;
    const text = extractAssistantText(last);
    emit("harness.result", {
      text,
      verified: false,
      execution_complete: true,
      note: "OMP agent settled; verified success requires explicit verifier evidence",
    });
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    if (ctx.agent?.kind === "main" && state.process) {
      await stopBrainstorm(ctx, "session_shutdown");
    }
  });
}

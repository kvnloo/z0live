/**
 * z0live OMP extension
 *
 * Drop/symlink this file into ~/.omp/agent/extensions/z0live.ts.
 *
 * OMP owns execution. z0live owns realtime conversation. z0intelligence owns
 * voice selection/admission. This extension is only the narrow harness bridge.
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer, type Server, type Socket } from "node:net";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

type JsonRecord = Record<string, unknown>;

interface BridgeState {
	server?: Server;
	socket?: Socket;
	port?: number;
	buffer: string;
	child?: ChildProcess;
	gateway?: WebSocket;
	tempDir?: string;
	planPath?: string;
	phase: "off" | "planning" | "warming" | "ready" | "error";
	lastContext?: ExtensionContext;
	lastError?: string;
	activeTraceId?: string;
}

const state: BridgeState = {
	buffer: "",
	phase: "off",
};

function textFromMessage(message: unknown): string {
	const m = message as { content?: unknown };
	if (!Array.isArray(m?.content)) return "";
	return m.content
		.filter((part): part is { type: "text"; text: string } => {
			if (!part || typeof part !== "object") return false;
			const p = part as { type?: unknown; text?: unknown };
			return p.type === "text" && typeof p.text === "string";
		})
		.map(part => part.text)
		.join("\n")
		.trim();
}

function updateStatus(ctx?: ExtensionContext): void {
	const current = ctx ?? state.lastContext;
	if (!current?.hasUI) return;
	if (state.phase === "off") {
		current.ui.setStatus("z0live", undefined);
		return;
	}
	const label =
		state.phase === "ready"
			? "🎙 brainstorm"
			: state.phase === "warming"
				? "🎙 warming"
				: state.phase === "planning"
					? "🎙 planning"
					: "🎙 error";
	current.ui.setStatus("z0live", current.ui.theme.fg(state.phase === "error" ? "error" : "accent", label));
}

function sendLine(payload: JsonRecord): void {
	const socket = state.socket;
	if (!socket || socket.destroyed) return;
	socket.write(JSON.stringify(payload) + "\n");
}

function emitTimeline(
	kind: string,
	payload: JsonRecord = {},
	traceId?: string,
	taskId?: string,
): void {
	sendLine({
		type: "event",
		event: {
			kind,
			source: "omp",
			at_ms: 0,
			payload,
			...(traceId ? { trace_id: traceId } : {}),
			...(taskId ? { task_id: taskId } : {}),
		},
	});
}

async function handleHarnessCommand(pi: ExtensionAPI, raw: JsonRecord): Promise<void> {
	const command = (raw.command ?? {}) as JsonRecord;
	const commandId = String(command.command_id ?? "");
	const kind = String(command.kind ?? "");
	const traceId = String(command.trace_id ?? "voice");
	const taskId = command.task_id == null ? undefined : String(command.task_id);
	const prompt = command.text == null ? "" : String(command.text);
	state.activeTraceId = traceId;

	const result = (ok: boolean, extra: JsonRecord = {}) => {
		sendLine({
			type: "result",
			command_id: commandId,
			result: { ok, ...extra },
		});
	};

	try {
		if (kind === "submit") {
			if (!prompt) return result(false, { error: "empty prompt" });
			pi.sendUserMessage(prompt, { deliverAs: "followUp" });
			result(true, { accepted: "followUp", trace_id: traceId, task_id: taskId });
			return;
		}
		if (kind === "steer") {
			if (!prompt) return result(false, { error: "empty prompt" });
			pi.sendUserMessage(prompt, { deliverAs: "steer" });
			result(true, { accepted: "steer", trace_id: traceId, task_id: taskId });
			return;
		}
		if (kind === "redirect") {
			if (!prompt) return result(false, { error: "empty prompt" });
			state.lastContext?.abort();
			pi.sendUserMessage(prompt);
			result(true, { accepted: "abort_and_prompt", trace_id: traceId, task_id: taskId });
			return;
		}
		if (kind === "cancel") {
			state.lastContext?.abort();
			result(true, { accepted: "abort", trace_id: traceId, task_id: taskId });
			return;
		}
		if (kind === "approve") {
			result(false, { error: "OMP extension API exposes approval events but no approval-resolution action" });
			return;
		}
		result(false, { error: `unsupported command: ${kind}` });
	} catch (error) {
		result(false, { error: String(error) });
	}
}

function handleBridgeChunk(pi: ExtensionAPI, chunk: Buffer): void {
	state.buffer += chunk.toString("utf8");
	for (;;) {
		const newline = state.buffer.indexOf("\n");
		if (newline < 0) break;
		const line = state.buffer.slice(0, newline).trim();
		state.buffer = state.buffer.slice(newline + 1);
		if (!line) continue;
		try {
			const raw = JSON.parse(line) as JsonRecord;
			if (raw.type === "command") void handleHarnessCommand(pi, raw);
		} catch (error) {
			pi.logger.warn("z0live bridge received invalid JSON", { error: String(error), line });
		}
	}
}

async function startHarnessBridge(pi: ExtensionAPI): Promise<number> {
	if (state.server && state.port) return state.port;
	const server = createServer(socket => {
		if (state.socket && !state.socket.destroyed) state.socket.destroy();
		state.socket = socket;
		state.buffer = "";
		socket.setNoDelay(true);
		socket.write(
			JSON.stringify({
				type: "hello",
				schema: "z0live.harness_bridge.v1",
				harness: "omp",
				capabilities: {
					submit: true,
					steer: true,
					redirect: true,
					cancel: true,
					approvals: false,
					progress_events: true,
					verified_results: false,
				},
			}) + "\n",
		);
		socket.on("data", chunk => handleBridgeChunk(pi, chunk));
		socket.on("error", error => {
			pi.logger.warn("z0live harness socket error", { error: String(error) });
		});
		socket.on("close", () => {
			if (state.socket === socket) state.socket = undefined;
		});
	});
	state.server = server;
	await new Promise<void>((resolve, reject) => {
		server.once("error", reject);
		server.listen(0, "127.0.0.1", () => resolve());
	});
	const address = server.address();
	if (!address || typeof address === "string") throw new Error("z0live harness bridge failed to bind TCP port");
	state.port = address.port;
	return address.port;
}

async function waitForGateway(timeoutMs = 180_000): Promise<WebSocket> {
	const port = Number(process.env.Z0LIVE_GATEWAY_PORT ?? 8765);
	const url = `ws://127.0.0.1:${port}`;
	const deadline = Date.now() + timeoutMs;
	let lastError: unknown;
	while (Date.now() < deadline) {
		try {
			const ws = new WebSocket(url);
			await new Promise<void>((resolve, reject) => {
				const timer = setTimeout(() => reject(new Error("gateway connect timeout")), 2_000);
				ws.addEventListener("open", () => {
					clearTimeout(timer);
					resolve();
				}, { once: true });
				ws.addEventListener("error", event => {
					clearTimeout(timer);
					reject(event);
				}, { once: true });
			});
			return ws;
		} catch (error) {
			lastError = error;
			await new Promise(resolve => setTimeout(resolve, 500));
		}
	}
	throw new Error(`z0live gateway did not become ready: ${String(lastError)}`);
}

function attachGatewayLogging(pi: ExtensionAPI, ws: WebSocket): void {
	ws.addEventListener("message", event => {
		if (typeof event.data !== "string") return;
		try {
			const raw = JSON.parse(event.data) as JsonRecord;
			if (raw.type === "hello") {
				pi.logger.debug("z0live gateway hello", raw);
			}
		} catch {
			// Audio/status clients can send non-JSON; ignore here.
		}
	});
	ws.addEventListener("close", () => {
		if (state.gateway === ws) {
			state.gateway = undefined;
			if (state.phase === "ready") {
				state.phase = "error";
				state.lastError = "z0live gateway disconnected";
				updateStatus();
			}
		}
	});
}

async function stopBrainstorm(ctx?: ExtensionContext): Promise<void> {
	state.gateway?.close();
	state.gateway = undefined;

	if (state.child && state.child.exitCode == null) {
		state.child.kill("SIGTERM");
		await new Promise<void>(resolve => {
			const timer = setTimeout(() => {
				if (state.child?.exitCode == null) state.child?.kill("SIGKILL");
				resolve();
			}, 5_000);
			state.child?.once("exit", () => {
				clearTimeout(timer);
				resolve();
			});
		});
	}
	state.child = undefined;

	state.socket?.destroy();
	state.socket = undefined;
	if (state.server) {
		await new Promise<void>(resolve => state.server!.close(() => resolve()));
	}
	state.server = undefined;
	state.port = undefined;

	if (state.tempDir) await rm(state.tempDir, { recursive: true, force: true }).catch(() => {});
	state.tempDir = undefined;
	state.planPath = undefined;
	state.phase = "off";
	state.lastError = undefined;
	state.activeTraceId = undefined;
	updateStatus(ctx);
}

async function startBrainstorm(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
	if (state.phase !== "off" && state.phase !== "error") {
		ctx.ui.notify(`Brainstorm already ${state.phase}.`, "info");
		return;
	}
	await stopBrainstorm(ctx);
	state.lastContext = ctx;
	state.phase = "planning";
	updateStatus(ctx);

	const bridgePort = await startHarnessBridge(pi);
	state.tempDir = await mkdtemp(join(tmpdir(), "omp-z0live-"));
	state.planPath = join(state.tempDir, "voice-plan.json");

	const voicePlanBin = process.env.Z0INT_VOICE_PLAN_BIN ?? "z0int-voice-plan";
	const plan = await pi.exec(
		voicePlanBin,
		["brainstorm", "--harness", "omp", "--pretty", "--output", state.planPath],
		{ timeout: 15_000 },
	);
	if (plan.code !== 0) {
		state.phase = "error";
		state.lastError = (plan.stderr || plan.stdout || `VoicePlan exited ${plan.code}`).trim();
		updateStatus(ctx);
		ctx.ui.notify(`Brainstorm admission refused:\n${state.lastError}`, "warning");
		return;
	}

	const z0liveBin = process.env.Z0LIVE_BIN ?? "z0live";
	state.phase = "warming";
	updateStatus(ctx);
	const gatewayPort = String(process.env.Z0LIVE_GATEWAY_PORT ?? "8765");
	const child = spawn(
		z0liveBin,
		[
			"serve",
			"--plan",
			state.planPath,
			"--listen",
			`127.0.0.1:${gatewayPort}`,
			"--harness",
			`127.0.0.1:${bridgePort}`,
		],
		{
			env: process.env,
			stdio: ["ignore", "pipe", "pipe"],
		},
	);
	state.child = child;
	child.stdout?.on("data", chunk => pi.logger.debug("z0live", { stream: "stdout", text: chunk.toString() }));
	child.stderr?.on("data", chunk => pi.logger.debug("z0live", { stream: "stderr", text: chunk.toString() }));
	child.once("exit", (code, signal) => {
		if (state.child !== child) return;
		state.child = undefined;
		if (state.phase !== "off") {
			state.phase = "error";
			state.lastError = `z0live exited code=${code} signal=${signal}`;
			updateStatus();
		}
	});

	try {
		const ws = await waitForGateway();
		state.gateway = ws;
		attachGatewayLogging(pi, ws);
		state.phase = "ready";
		updateStatus(ctx);
		ctx.ui.notify("Brainstorm ready. z0live is warm; OMP remains the execution authority.", "info");
	} catch (error) {
		state.phase = "error";
		state.lastError = String(error);
		updateStatus(ctx);
		ctx.ui.notify(`Brainstorm failed: ${state.lastError}`, "error");
	}
}

function registerHarnessEvents(pi: ExtensionAPI): void {
	const capture = (ctx: ExtensionContext) => {
		state.lastContext = ctx;
	};

	pi.on("session_start", async (_event, ctx) => {
		capture(ctx);
		updateStatus(ctx);
		if (pi.getFlag("brainstorm") === true) await startBrainstorm(pi, ctx);
	});

	pi.on("turn_start", async (event, ctx) => {
		capture(ctx);
		emitTimeline("harness.progress", {
			text: "OMP turn started",
			phase: "worker_ready",
			turn_index: event.turnIndex,
		}, state.activeTraceId);
	});

	pi.on("tool_call", async (event, ctx) => {
		capture(ctx);
		emitTimeline("harness.progress", {
			text: `OMP tool started: ${event.toolName}`,
			tool_name: event.toolName,
			tool_call_id: event.toolCallId,
		}, state.activeTraceId);
	});

	pi.on("tool_result", async (event, ctx) => {
		capture(ctx);
		emitTimeline(
			event.isError ? "harness.error" : "harness.progress",
			{
				text: event.isError ? `OMP tool failed: ${event.toolName}` : `OMP tool finished: ${event.toolName}`,
				tool_name: event.toolName,
				tool_call_id: event.toolCallId,
				is_error: event.isError,
			},
			state.activeTraceId,
		);
	});

	pi.on("tool_approval_requested", async (event, ctx) => {
		capture(ctx);
		emitTimeline("harness.approval", {
			text: `Approval required for ${event.toolName}`,
			tool_name: event.toolName,
			tool_call_id: event.toolCallId,
			reason: event.reason,
			approval_mode: event.approvalMode,
		}, state.activeTraceId);
	});

	pi.on("tool_approval_resolved", async (event, ctx) => {
		capture(ctx);
		emitTimeline("harness.progress", {
			text: `Approval ${event.approved ? "granted" : "denied"} for ${event.toolName}`,
			tool_name: event.toolName,
			tool_call_id: event.toolCallId,
			approved: event.approved,
		}, state.activeTraceId);
	});

	pi.on("turn_end", async (event, ctx) => {
		capture(ctx);
		const text = textFromMessage(event.message);
		emitTimeline("harness.result", {
			text,
			verified: false,
			turn_index: event.turnIndex,
			note: "OMP turn completion is not equivalent to verified success",
		}, state.activeTraceId);
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		capture(ctx);
		await stopBrainstorm(ctx);
	});
}

export default function z0liveExtension(pi: ExtensionAPI) {
	pi.registerFlag("brainstorm", {
		description: "Start the session in on-demand z0live Brainstorm Mode",
		type: "boolean",
		default: false,
	});

	pi.registerCommand("brainstorm", {
		description: "Start/stop/status for on-demand z0live Brainstorm Mode",
		handler: async (args, ctx) => {
			state.lastContext = ctx;
			const action = args.trim().toLowerCase() || "toggle";
			if (action === "status") {
				let plan = "";
				if (state.planPath) {
					try {
						plan = await readFile(state.planPath, "utf8");
					} catch {
						plan = "";
					}
				}
				ctx.ui.notify(
					[
						`z0live: ${state.phase}`,
						state.lastError ? `error: ${state.lastError}` : "",
						plan ? `VoicePlan: ${plan.slice(0, 1000)}` : "",
					]
						.filter(Boolean)
						.join("\n"),
					state.phase === "error" ? "error" : "info",
				);
				return;
			}
			if (action === "off" || action === "stop" || (action === "toggle" && state.phase !== "off")) {
				await stopBrainstorm(ctx);
				ctx.ui.notify("Brainstorm stopped; heavyweight voice resources released.", "info");
				return;
			}
			if (action === "on" || action === "start" || action === "toggle") {
				await startBrainstorm(pi, ctx);
				return;
			}
			ctx.ui.notify("Usage: /brainstorm [on|off|status]", "warning");
		},
	});

	registerHarnessEvents(pi);
}

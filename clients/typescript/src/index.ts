/** Desktop HTTP /v1 broker adapter; NOT the portable z0live WebSocket protocol. */

export type ClientKind = "lawnchair" | "assistant" | "dash" | "web";
export type EventType =
  | "user_transcript" | "assistant_transcript" | "tool_call" | "tool_result"
  | "summary" | "feedback" | "app_state";
export type BuiltinName = "tell_hermes" | "get_context" | "summarize";
export type JsonObject = Record<string, unknown>;
export type FetchTransport = (input: string, init: RequestInit) => Promise<Response>;
export type TokenProvider = () => string | Promise<string>;
export interface CursorStore {
  load(): number | null | Promise<number | null>;
  save(after: number): void | Promise<void>;
}
export interface Options {
  /** Exact expected HTTPS origin, not a general URL or redirect target. */
  origin: string;
  token: TokenProvider;
  fetch: FetchTransport;
  /** Epoch seconds supplied by the host, not a global Date dependency. */
  nowSeconds: () => number;
  cursorStore?: CursorStore;
  timeoutMs?: number;
}
export interface AppTool {
  type: "function";
  name: string;
  description?: string;
  parameters: JsonObject;
}
export interface MintRequest {
  client: ClientKind;
  context?: JsonObject;
  tools?: AppTool[];
}
export interface MintResult {
  session_id: string;
  value: string; // ephemeral: caller must never persist this credential
  expires_at: number; // provider Unix epoch seconds
  model: string;
  voice: string;
  instructions_version: string;
}
export interface Event {
  session_id: string;
  type: EventType;
  text?: string;
  ts?: number | string; // producer Unix seconds or ISO time; never receipt time
  data?: JsonObject;
}
export interface InboxMessage {
  id: number;
  say: string;
  action: string | null;
  params: JsonObject;
  session_id: string | null;
}
export interface InboxPage {
  messages: InboxMessage[];
  after: number;
  has_more: boolean;
}
export interface Session {
  id: string;
  client: ClientKind;
  created: number;
  updated: number;
  ended: 0 | 1;
}
export interface ContextResult {
  session_id: string;
  client: ClientKind;
  context: JsonObject;
}
export interface QueueResult {
  ok: true;
  id: number;
  status: "queued"; // never implies a completed Hermes action
}
export interface SummaryResult {
  ok: true;
  status: "queued";
}
export interface SSEMessage {
  id: number;
  event: "message";
  data: InboxMessage;
}

export type ErrorKind = "configuration" | "protocol" | "auth" | "http" |
  "network" | "aborted" | "timeout";
export class BrokerError extends Error {
  constructor(
    public readonly kind: ErrorKind,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "BrokerError";
  }
}

const clients: ClientKind[] = ["lawnchair", "assistant", "dash", "web"];
const eventTypes: EventType[] = [
  "user_transcript", "assistant_transcript", "tool_call", "tool_result",
  "summary", "feedback", "app_state",
];
const sessionId = /^[A-Za-z0-9_-]{1,100}$/;
const functionName = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const builtinNames = new Set<BuiltinName>(["tell_hermes", "get_context", "summarize"]);

function fail(message: string): never { throw new BrokerError("protocol", message); }
function record(value: unknown, name: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail(name + " must be an object");
  return value as JsonObject;
}
function string(value: unknown, name: string, mayBeEmpty = false): string {
  if (typeof value !== "string" || (!mayBeEmpty && value.length === 0)) fail(name + " must be a string");
  return value as string;
}
function bool(value: unknown, name: string): boolean {
  if (typeof value !== "boolean") fail(name + " must be a boolean");
  return value;
}
function finite(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(name + " must be a finite number");
  return value as number;
}
function integer(value: unknown, name: string, min = 0): number {
  const n = finite(value, name);
  if (!Number.isSafeInteger(n) || n < min) fail(name + " must be a safe nonnegative integer");
  return n;
}
function id(value: unknown, name: string): string {
  const result = string(value, name);
  if (!sessionId.test(result)) fail(name + " is malformed");
  return result;
}
function client(value: unknown): ClientKind {
  if (!clients.includes(value as ClientKind)) fail("invalid client");
  return value as ClientKind;
}
function ok(value: unknown): JsonObject {
  const obj = record(value, "response");
  if (obj.ok !== true) fail("response.ok must equal true");
  return obj;
}
function parseJson(text: string): unknown {
  try { return JSON.parse(text) as unknown; }
  catch { return fail("invalid JSON response"); }
}
function parseMessage(value: unknown): InboxMessage {
  const obj = record(value, "inbox message");
  const messageId = integer(obj.id, "message.id", 1);
  const say = string(obj.say, "message.say", true);
  const action = obj.action === null ? null : string(obj.action, "message.action");
  const params = record(obj.params, "message.params");
  const sid = obj.session_id === null ? null : id(obj.session_id, "message.session_id");
  return { id: messageId, say, action, params, session_id: sid };
}
function parsePage(value: unknown): InboxPage {
  const obj = record(value, "inbox");
  if (!Array.isArray(obj.messages) || obj.messages.length > 100) fail("inbox.messages must be an array of <=100");
  const messages = obj.messages.map(parseMessage);
  const after = integer(obj.after, "inbox.after");
  const has_more = bool(obj.has_more, "inbox.has_more");
  let previous = 0;
  for (const item of messages) {
    if (item.id <= previous || item.id > after) fail("nonmonotonic inbox ids or invalid cursor");
    previous = item.id;
  }
  if (messages.length > 0 && after !== previous) fail("inbox.after must equal last message id");
  return { messages, after, has_more };
}
function parseSession(value: unknown): Session {
  const obj = record(value, "session");
  const ended = integer(obj.ended, "session.ended");
  if (ended !== 0 && ended !== 1) fail("session.ended must be 0 or 1");
  return {
    id: id(obj.id, "session.id"), client: client(obj.client),
    created: finite(obj.created, "session.created"),
    updated: finite(obj.updated, "session.updated"),
    ended: ended as 0 | 1,
  };
}
function parseMint(value: unknown): MintResult {
  const obj = record(value, "mint");
  return {
    session_id: id(obj.session_id, "session_id"),
    value: string(obj.value, "ephemeral secret"),
    expires_at: finite(obj.expires_at, "expires_at"),
    model: string(obj.model, "model"),
    voice: string(obj.voice, "voice"),
    instructions_version: string(obj.instructions_version, "instructions_version"),
  };
}
function parseContext(value: unknown): ContextResult {
  const obj = record(value, "context");
  return { session_id: id(obj.session_id, "session_id"), client: client(obj.client),
    context: record(obj.context, "context") };
}
function parseQueue(value: unknown): QueueResult {
  const obj = ok(value);
  if (obj.status !== "queued") fail("tool result is not queued");
  return { ok: true, status: "queued", id: integer(obj.id, "job.id", 1) };
}
function parseSummary(value: unknown): SummaryResult {
  const obj = ok(value);
  if (obj.status !== "queued") fail("summary result is not queued");
  return { ok: true, status: "queued" };
}
function requireOrigin(value: string): string {
  let origin: URL;
  try { origin = new URL(value); }
  catch { return fail("invalid HTTPS broker origin"); }
  if (origin.protocol !== "https:" || origin.username || origin.password ||
      origin.origin !== value || origin.pathname !== "/" || origin.search || origin.hash) {
    throw new BrokerError("configuration", "broker must be an exact HTTPS origin");
  }
  return origin.origin;
}
function validateMintRequest(request: MintRequest): void {
  client(request.client);
  if (request.context !== undefined) {
    const context = record(request.context, "request.context");
    if (JSON.stringify(context).length > 16000) fail("context exceeds 16000 characters");
  }
  if (request.tools !== undefined) {
    if (!Array.isArray(request.tools) || request.tools.length > 32 ||
        JSON.stringify(request.tools).length > 48000) fail("invalid app tool limits");
    const names = new Set<string>();
    for (const tool of request.tools) {
      const t = record(tool, "tool");
      const name = string(t.name, "tool.name");
      if (t.type !== "function" || !functionName.test(name) ||
          builtinNames.has(name as BuiltinName) || names.has(name) ||
          record(t.parameters, "tool.parameters").type !== "object") {
        fail("invalid/duplicate or reserved app tool");
      }
      names.add(name);
    }
  }
}
function validateEvent(event: Event): void {
  const item = record(event, "event");
  id(item.session_id, "event.session_id");
  if (!eventTypes.includes(item.type as EventType)) fail("unknown event type");
  if (item.text !== undefined && string(item.text, "event.text", true).length > 16000) fail("event text too long");
  if (item.ts !== undefined && (
      typeof item.ts === "number" ? !Number.isFinite(item.ts) :
      typeof item.ts === "string" ? !Number.isFinite(Date.parse(item.ts)) : true)) {
    fail("event.ts must be finite Unix seconds or a valid ISO string");
  }
  if (item.data !== undefined) {
    const data = record(item.data, "event.data");
    if (data.event_id !== undefined &&
      (typeof data.event_id !== "string" || data.event_id.length > 200)) fail("invalid event_id");
  }
  if (JSON.stringify(item).length > 32000) fail("event limit 32000 characters");
}

/** Scoped in-memory bearer; callers should clear when unpairing and never log tokens. */
export function memoryToken(initial: string): {
  provider: TokenProvider; replace(value: string): void; clear(): void;
} {
  let secret = string(initial, "token");
  return {
    provider: () => secret,
    replace: value => { secret = string(value, "token"); },
    clear: () => { secret = ""; },
  };
}

export class DesktopBrokerClient {
  private readonly origin: string;
  private readonly token: TokenProvider;
  private readonly fetchImpl: FetchTransport;
  private readonly nowSeconds: () => number;
  private readonly cursorStore: CursorStore | undefined;
  private readonly timeoutMs: number;

  constructor(options: Options) {
    this.origin = requireOrigin(options.origin);
    this.token = options.token;
    this.fetchImpl = options.fetch;
    this.nowSeconds = options.nowSeconds;
    this.cursorStore = options.cursorStore;
    this.timeoutMs = options.timeoutMs ?? 10000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs < 1) {
      throw new BrokerError("configuration", "invalid timeoutMs");
    }
  }

  private async open(method: "GET" | "POST", path: string, body: unknown,
      signal?: AbortSignal): Promise<{ response: Response; cleanup: () => void }> {
    const controller = new AbortController();
    let timedOut = false;
    let cancelled = signal?.aborted ?? false;
    const abort = () => { cancelled = true; controller.abort(); };
    if (signal) signal.addEventListener("abort", abort, { once: true });
    if (cancelled) controller.abort();
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, this.timeoutMs);
    const cleanup = () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", abort);
    };
    try {
      // Origin comes from validated constructor; paths are internal literals only.
      const token = string(await this.token(), "device bearer");
      const response = await this.fetchImpl(this.origin + path, {
        method, redirect: "manual", credentials: "omit", cache: "no-store",
        headers: {
          Authorization: "Bearer " + token,
          ...(method === "POST" ? { "Content-Type": "application/json" } : {}),
        },
        ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
        signal: controller.signal,
      });
      if (response.redirected || (response.status >= 300 && response.status < 400)) {
        throw new BrokerError("http", "redirect refused", response.status);
      }
      return { response, cleanup };
    } catch (error) {
      cleanup();
      if (error instanceof BrokerError) throw error;
      if (timedOut) throw new BrokerError("timeout", "broker request timed out");
      if (cancelled) throw new BrokerError("aborted", "broker request aborted");
      throw new BrokerError("network", "broker unavailable");
    }
  }

  private async json<T>(method: "GET" | "POST", path: string, body: unknown,
      parser: (v: unknown) => T, signal?: AbortSignal, retries = 0): Promise<T> {
    // At most two retries for explicitly requested idempotent GET reads.
    const bounded = method === "GET" ? Math.min(2, Math.max(0, Math.trunc(retries))) : 0;
    for (let attempt = 0; ; attempt++) {
      try {
        const { response, cleanup } = await this.open(method, path, body, signal);
        try {
          const raw = await response.text();
          if (!response.ok) {
            // Do not reflect server body or token into exception text.
            throw new BrokerError(response.status === 401 ? "auth" : "http",
              response.status === 401 ? "device bearer rejected" : "broker HTTP error",
              response.status);
          }
          return parser(parseJson(raw));
        } finally { cleanup(); }
      } catch (error) {
        const err = error instanceof BrokerError ? error :
          new BrokerError(signal?.aborted ? "aborted" : "network", "broker unavailable");
        if (attempt >= bounded || signal?.aborted ||
            !(err.kind === "network" || err.kind === "timeout" ||
              (err.kind === "http" && err.status !== undefined && err.status >= 500))) {
          throw err;
        }
      }
    }
  }

  async mint(request: MintRequest, signal?: AbortSignal): Promise<MintResult> {
    validateMintRequest(request);
    const result = await this.json("POST", "/v1/realtime/session", request, parseMint, signal);
    if (result.expires_at <= finite(this.nowSeconds(), "nowSeconds")) {
      throw new BrokerError("protocol", "minted ephemeral credential has already expired");
    }
    return result;
  }

  async callBuiltin(name: "tell_hermes", sid: string,
      args: { text: string; kind: "request" | "complaint" | "bug" | "praise" },
      signal?: AbortSignal): Promise<QueueResult>;
  async callBuiltin(name: "get_context", sid: string, args?: JsonObject,
      signal?: AbortSignal): Promise<ContextResult>;
  async callBuiltin(name: "summarize", sid: string,
      args: { text: string }, signal?: AbortSignal): Promise<SummaryResult>;
  async callBuiltin(name: BuiltinName, sid: string,
      args: JsonObject = {}, signal?: AbortSignal): Promise<QueueResult | ContextResult | SummaryResult> {
    id(sid, "session_id");
    record(args, "arguments");
    if (name === "tell_hermes") {
      string(args.text, "tell_hermes.text");
      if (!["request", "complaint", "bug", "praise"].includes(args.kind as string)) {
        fail("invalid tell_hermes.kind");
      }
    } else if (name === "summarize") {
      string(args.text, "summarize.text");
    }
    const parse = name === "tell_hermes" ? parseQueue :
      name === "summarize" ? parseSummary : parseContext;
    return this.json("POST", "/v1/tools/" + name, {
      session_id: sid, arguments: args,
    }, parse, signal);
  }

  async postEvents(events: Event[], signal?: AbortSignal): Promise<number> {
    if (!Array.isArray(events) || events.length < 1 || events.length > 100) {
      fail("event batch must contain 1..100 records");
    }
    for (const event of events) validateEvent(event);
    const result = await this.json("POST", "/v1/events", events, ok, signal);
    const accepted = integer(result.accepted, "accepted");
    if (accepted > events.length) fail("accepted exceeds submitted event count");
    return accepted;
  }

  async getInbox(after?: number, options?: { signal?: AbortSignal; retries?: number }): Promise<InboxPage> {
    let cursor = after;
    if (cursor === undefined) cursor = (await this.cursorStore?.load()) ?? 0;
    cursor = integer(cursor, "after");
    const page = await this.json("GET", "/v1/inbox?after=" + cursor, undefined,
      parsePage, options?.signal, options?.retries);
    if (page.messages.some(msg => msg.id <= cursor) || page.after < cursor) {
      fail("inbox response regressed past requested cursor");
    }
    return page;
  }

  /** Persist only after a caller has actually processed the item. No auto-ack on reading. */
  async acknowledge(message: InboxMessage): Promise<boolean> {
    const parsed = parseMessage(message);
    if (!this.cursorStore) throw new BrokerError("configuration", "no injected cursor store");
    const previous = this.cursorStore.load();
    const old = (await previous) ?? 0;
    integer(old, "stored cursor");
    if (parsed.id <= old) return false;
    await this.cursorStore.save(parsed.id);
    return true;
  }

  async listSessions(signal?: AbortSignal): Promise<Session[]> {
    return this.json("GET", "/v1/sessions", undefined, value => {
      const obj = record(value, "sessions");
      if (!Array.isArray(obj.sessions)) fail("sessions must be an array");
      return obj.sessions.map(parseSession);
    }, signal);
  }

  async getContext(sid: string, signal?: AbortSignal): Promise<ContextResult> {
    id(sid, "session_id");
    return this.json("GET", "/v1/context?session_id=" + encodeURIComponent(sid),
      undefined, parseContext, signal);
  }

  async endSession(sid: string, signal?: AbortSignal): Promise<{ ok: true; id: number | null }> {
    id(sid, "session_id");
    return this.json("POST", "/v1/sessions/" + sid + "/end", {}, value => {
      const obj = ok(value);
      return { ok: true as const, id: obj.id === null ? null : integer(obj.id, "end.id", 1) };
    }, signal);
  }

  async *stream(after = 0, signal?: AbortSignal): AsyncGenerator<SSEMessage> {
    let cursor = integer(after, "stream.after");
    const { response, cleanup } = await this.open(
      "GET", "/v1/stream?after=" + cursor, undefined, signal);
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      if (!response.ok) {
        throw new BrokerError(response.status === 401 ? "auth" : "http",
          response.status === 401 ? "device bearer rejected" : "broker SSE HTTP error",
          response.status);
      }
      if (!response.headers.get("Content-Type")?.startsWith("text/event-stream") || !response.body) {
        fail("stream must be text/event-stream with a body");
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let pending = "";
      let done = false;
      while (!done) {
        const chunk = await reader.read();
        done = chunk.done;
        pending += decoder.decode(chunk.value, { stream: !done });
        // Preserve a trailing isolated CR until the next chunk.
        pending = pending.replace(/\r\n/g, "\n");
        if (pending.length > 131072) fail("SSE message buffer exceeded");
        let boundary = pending.indexOf("\n\n");
        while (boundary >= 0) {
          const block = pending.slice(0, boundary);
          pending = pending.slice(boundary + 2);
          boundary = pending.indexOf("\n\n");
          const fields = block.split("\n");
          let event = "message";
          let seenId: string | undefined;
          const data: string[] = [];
          for (const line of fields) {
            if (line.startsWith(":") || !line) continue;
            if (line.startsWith("event:")) event = line.slice(6).trimStart();
            else if (line.startsWith("id:")) seenId = line.slice(3).trimStart();
            else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
          }
          if (data.length === 0 || event !== "message") continue;
          if (!seenId || !/^(0|[1-9][0-9]*)$/.test(seenId)) fail("invalid SSE id");
          const parsedId = integer(Number(seenId), "SSE id", 1);
          const message = parseMessage(parseJson(data.join("\n")));
          if (message.id !== parsedId || parsedId <= cursor) fail("SSE id/data/cursor mismatch");
          cursor = parsedId;
          yield { event: "message", id: parsedId, data: message };
        }
      }
      if (pending.trim().length !== 0) fail("truncated SSE message");
    } catch (error) {
      if (error instanceof BrokerError) throw error;
      throw new BrokerError(signal?.aborted ? "aborted" : "network", "broker SSE interrupted");
    } finally {
      if (reader) {
        try { await reader.cancel(); } catch { /* transport teardown */ }
      }
      cleanup();
    }
  }
}

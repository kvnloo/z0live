import test from "node:test";
import assert from "node:assert/strict";
import {
  executeHarnessCommand,
  extractAssistantText,
  extractTextContent,
  verifiedFromDetails,
} from "./index.mjs";

test("extracts text blocks without leaking non-text content", () => {
  assert.equal(
    extractTextContent([
      { type: "text", text: "hello" },
      { type: "image", data: "secret" },
      { type: "text", text: "world" },
    ]),
    "hello\nworld",
  );
});

test("assistant text extraction is shape-tolerant", () => {
  assert.equal(extractAssistantText({ content: [{ type: "text", text: "done" }] }), "done");
  assert.equal(extractAssistantText(null), "");
});

test("only explicit verifier fields count as verified", () => {
  assert.equal(verifiedFromDetails({ verified_success: true }), true);
  assert.equal(verifiedFromDetails({ verified: true }), true);
  assert.equal(verifiedFromDetails({ result: { verified_success: true } }), true);
  assert.equal(verifiedFromDetails({ success: true }), false);
  assert.equal(verifiedFromDetails(null), false);
});

test("harness commands preserve steer/follow-up/cancel semantics", async () => {
  const calls = [];
  const pi = {
    sendUserMessage(text, options) {
      calls.push(["send", text, options]);
    },
  };
  let aborted = 0;
  const ctx = {
    agent: { kind: "main" },
    isIdle: () => false,
    abort: () => {
      aborted += 1;
    },
  };

  await executeHarnessCommand({ kind: "submit", text: "a", trace_id: "t" }, pi, ctx);
  await executeHarnessCommand({ kind: "steer", text: "b", trace_id: "t" }, pi, ctx);
  await executeHarnessCommand({ kind: "cancel", trace_id: "t" }, pi, ctx);

  assert.deepEqual(calls[0], ["send", "a", { deliverAs: "followUp", attribution: "user" }]);
  assert.deepEqual(calls[1], ["send", "b", { deliverAs: "steer", attribution: "user" }]);
  assert.equal(aborted, 1);
});

test("approval command reports unsupported instead of pretending", async () => {
  const result = await executeHarnessCommand(
    { kind: "approve", trace_id: "t" },
    {},
    { agent: { kind: "main" } },
  );
  assert.equal(result.ok, false);
  assert.match(result.unsupported, /approval/);
});

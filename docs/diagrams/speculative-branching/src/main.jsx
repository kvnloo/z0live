// z0live speculative branching (docs/SPECULATIVE-BRANCHING.md) as an XY Flow diagram. Same content as the Mermaid
// diagrams there; colours follow the z0live film palette. Status labels: built / proposal / falsified.
import React from "react";
import { createRoot } from "react-dom/client";
import { ReactFlow, Background, Controls, MiniMap, MarkerType } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

const C = { ink: "#d1deda", mute: "#929b98", pit: "#12171a", lift: "#252c30", you: "#98b39e", voice: "#c8be72",
  fast: "#4fa5b3", medium: "#99a362", slow: "#678c3e", hermes: "#d68064", ctx: "#8f86c9", bad: "#b55a5a" };

const box = (color, status) => ({
  background: C.lift, color: C.ink, border: `1.5px solid ${color}`, borderRadius: 14, padding: 10, width: 210,
  fontFamily: "Inter, system-ui, sans-serif", fontSize: 13, lineHeight: 1.35,
  opacity: status === "falsified" ? 0.55 : 1, borderStyle: status === "proposal" ? "dashed" : "solid",
});
const label = (title, sub, status) => (
  <div>
    <div style={{ fontWeight: 600 }}>{title}</div>
    {sub && <div style={{ color: C.mute, fontSize: 12, marginTop: 3 }}>{sub}</div>}
    {status && <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 10, letterSpacing: "0.06em",
      color: status === "built" ? C.fast : status === "falsified" ? C.bad : C.voice, marginTop: 6 }}>
      {status.toUpperCase()}</div>}
  </div>
);
const group = (id, title, x, y, w, h) => ({ id, type: "group", position: { x, y }, data: {},
  style: { width: w, height: h, background: "rgba(37,44,48,0.35)", border: `1px solid ${C.lift}`, borderRadius: 18 } });
const g = (id, parentId, x, y, title, sub, color, status) => ({ id, parentId, extent: "parent", position: { x, y },
  data: { label: label(title, sub, status) }, style: box(color, status) });
const n = (id, x, y, title, sub, color, status) => ({ id, position: { x, y }, data: { label: label(title, sub, status) },
  style: box(color, status) });

const nodes = [
  n("mic", 0, 260, "The user's mic (S25)", "WebRTC to the Studio adapter", C.you, "built"),
  n("asr", 270, 140, "Live transcript", "word-by-word partials, ~0.1-0.4 s behind speech", C.you, "built"),
  n("voice", 270, 420, "Voice model", "GPT-Live (default) · Grok · local Liquid", C.voice, "built"),
  group("spec", "While the user is still talking", 540, 0, 520, 470),
  { id: "spec-title", parentId: "spec", position: { x: 16, y: 10 }, draggable: false, selectable: false,
    data: { label: <span style={{ color: C.voice, fontFamily: "IBM Plex Mono, monospace", fontSize: 11 }}>WHILE KEVIN IS STILL TALKING</span> },
    style: { background: "transparent", border: "none", width: 300, padding: 0 } },
  g("fork", "spec", 20, 50, "Fork at each pause > 250 ms", "only with enough new words", C.voice, "proposal"),
  g("b1", "spec", 20, 170, "Branch A", "fast lane guesses the ending + drafts an answer", C.fast, "proposal"),
  g("b2", "spec", 280, 170, "Branch B", "another fast lane, different guess (personal intent model)", C.fast, "proposal"),
  g("b3", "spec", 20, 300, "Branch C", "answer the words so far (offline test: 33-43% right)", C.fast, "proposal"),
  g("rs", "spec", 280, 300, "Research lane (gather)", "read-only lookups; writes facts to the blackboard", C.medium, "proposal"),
  n("eot", 1120, 120, "End of turn", "VAD + semantic check", C.voice, "built"),
  n("judge", 1120, 260, "Coalesce", "which branch matches what he said? local, < 50 ms", C.voice, "proposal"),
  n("race", 1400, 400, "Fallback: tier race", "fast (3× Haiku) · medium · slow", C.fast, "built"),
  n("inject", 1400, 200, "Inject into the voice", "after end of turn only (early-speech gate)", C.voice, "built"),
  n("hermes", 1680, 400, "Hermes agent", "real work, lands last; per-call session (tek's fix)", C.hermes, "built"),
  n("ctx", 820, 560, "Shared context", "chat brief · live transcript · blackboard", C.ctx, "built"),
  n("eager", 1120, 0, "Launch at final transcript", "falsified: final arrives 0.42 s AFTER hand-off", C.bad, "falsified"),
  n("spk", 0, 460, "The user hears the answer", "", C.you, "built"),
];
const e = (id, s, t, opts = {}) => ({ id, source: s, target: t, animated: !!opts.animated, label: opts.label,
  style: { stroke: opts.color || C.mute, strokeWidth: 1.5, strokeDasharray: opts.dashed ? "6 5" : undefined },
  labelStyle: { fill: C.ink, fontSize: 11 }, labelBgStyle: { fill: C.pit },
  markerEnd: { type: MarkerType.ArrowClosed, color: opts.color || C.mute } });
const edges = [
  e("1", "mic", "asr", { animated: true, color: C.you }), e("2", "mic", "voice", { animated: true, color: C.you }),
  e("3", "asr", "fork", { color: C.you }), e("4", "fork", "b1"), e("5", "fork", "b2"), e("6", "fork", "b3"), e("7", "fork", "rs"),
  e("8", "asr", "eot", { color: C.you }), e("9", "eot", "judge"),
  e("10", "b1", "judge", { dashed: true }), e("11", "b2", "judge", { dashed: true }), e("12", "b3", "judge", { dashed: true }),
  e("13", "judge", "inject", { label: "match", color: C.fast }), e("14", "judge", "race", { label: "no match", color: C.voice }),
  e("15", "race", "inject"), e("16", "race", "hermes"), e("17", "hermes", "inject", { label: "adds something new", color: C.hermes }),
  e("18", "inject", "voice", { color: C.voice }), e("19", "voice", "spk", { animated: true, color: C.voice }),
  e("20", "ctx", "b1", { dashed: true, color: C.ctx }), e("21", "ctx", "race", { dashed: true, color: C.ctx }),
  e("22", "ctx", "hermes", { dashed: true, color: C.ctx }), e("23", "rs", "ctx", { color: C.medium }),
];

function App() {
  return (
    <div style={{ width: "100vw", height: "100vh" }}>
      <div style={{ position: "absolute", zIndex: 5, left: 20, top: 14, color: C.ink, fontFamily: "Inter, system-ui" }}>
        <div style={{ fontSize: 22 }}>z0live · speculative branching</div>
        <div style={{ color: C.mute, fontSize: 12 }}>solid = built · dashed = proposal · faded = falsified by measurement. Source: docs/SPECULATIVE-BRANCHING.md</div>
      </div>
      <ReactFlow nodes={nodes} edges={edges} fitView colorMode="dark" proOptions={{ hideAttribution: true }}
        style={{ background: C.pit }}>
        <Background color={C.lift} gap={24} />
        <MiniMap pannable zoomable style={{ background: C.lift }} nodeColor={() => C.mute} />
        <Controls />
      </ReactFlow>
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);

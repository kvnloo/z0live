#!/usr/bin/env python3
"""C56-brief dry check (no model, no network): the per-arm suite wiring in lanespeed.py.

  python3 lanespeed_c56brief_check.py        # exit 0 = all checks pass

1. slm-v2-e5.json is exactly what lane_brief_e5.py builds from the frozen slm-v2.json.
2. Both suites share identical cases and every non-brief lane-request field; only the brief text differs.
3. End to end with a fake llama-server (this file, re-invoked with llama-server flags): blocks A:512,B:512,A:512,B:512
   send the full brief in A blocks and the e5 brief in B blocks, -ub 512 in every block, and the receipt records the
   per-arm suite sha256 and block suite names. The fake server answers every request with one sentence.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fake_server(argv):
    port = int(argv[argv.index("--port") + 1])
    ub = argv[argv.index("-ub") + 1]

    class H(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - base signature
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            system = body["messages"][0]["content"]
            print(json.dumps({"ub": ub, "system_sha": hashlib.sha256(system.encode()).hexdigest()[:16],
                              "system_chars": len(system), "cache_prompt": body.get("cache_prompt")}), flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("It is ", "fine. ", "Done."):
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            last = {"choices": [{"delta": {}}], "timings": {"prompt_n": len(system) // 4, "prompt_ms": 1.0,
                                                           "predicted_n": 3, "predicted_ms": 1.0}}
            self.wfile.write(f"data: {json.dumps(last)}\n\ndata: [DONE]\n\n".encode())

    HTTPServer(("127.0.0.1", port), H).serve_forever()


def main():
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent))
    import lane_brief_e5
    import slm_bench

    full = json.loads((HERE / "slm-v2.json").read_text())
    e5 = json.loads((HERE / "slm-v2-e5.json").read_text())
    assert (HERE / "slm-v2-e5.json").read_text() == lane_brief_e5.render(full), "e5 suite stale"
    assert e5["cases"] == full["cases"] and len(full["cases"]) == 30
    assert {k: v for k, v in e5.items() if k not in ("brief", "suite", "note", "derived")} == \
        {k: v for k, v in full.items() if k not in ("brief", "suite", "note", "derived")}
    for case in full["cases"]:
        (sa, ua), (sb, ub_) = slm_bench.messages(full, case), slm_bench.messages(e5, case)
        assert ua == ub_ and sa.replace(full["brief"], e5["brief"]) == sb, case["id"]
    print("ok static: cases identical, briefs", len(full["brief"]), "->", len(e5["brief"]), "chars")

    with tempfile.TemporaryDirectory() as td:
        wrapper = Path(td) / "llama-server"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" "$@"\n')
        wrapper.chmod(0o755)
        env = {**os.environ, "LLAMA_SERVER": str(wrapper)}
        out = Path(td) / "r.json"
        subprocess.run([sys.executable, str(HERE / "lanespeed.py"), "--gguf", str(HERE / "slm-v2.json"),
                        "--blocks", "A:512,B:512,A:512,B:512", "--every", "2", "--deadline-s", "600",
                        "--suite-b", str(HERE / "slm-v2-e5.json"), "--out", str(out), "--logdir", td],
                       env=env, check=True)
        r = json.loads(out.read_text())
        assert r["complete"] and [b["label"] for b in r["blocks"]] == ["A1", "B1", "A2", "B2"]
        assert r["arm_suite"]["A"]["sha256"] == r["suite_sha256"]
        assert r["arm_suite"]["B"]["sha256"] == hashlib.sha256((HERE / "slm-v2-e5.json").read_bytes()).hexdigest()
        assert [b["suite"] for b in r["blocks"]] == ["slm-v2", "slm-v2-e5", "slm-v2", "slm-v2-e5"]
        assert all(b["ub"] == 512 and b["summary"]["n"] == 15 and not b["summary"]["errors"] for b in r["blocks"])
        logs = {}
        for b in r["blocks"]:
            log = (Path(td) / f"server-{b['block']}-{b['label']}.log").read_text()
            lines = [json.loads(x) for x in log.splitlines() if x.startswith("{")]
            assert len(lines) == 16 and all(x["ub"] == "512" and x["cache_prompt"] is False for x in lines)
            logs[b["label"]] = [x["system_sha"] for x in lines]
        assert logs["A1"] == logs["A2"] and logs["B1"] == logs["B2"]
        assert not set(logs["A1"]) & set(logs["B1"])
        verdict = subprocess.run([sys.executable, str(HERE / "lanespeed_verdict.py"), str(out)], check=True,
                                 capture_output=True, text=True).stdout
        assert json.loads(verdict)["verdict"] in ("KEEP", "NO_GAIN", "REVERT")
    print("ok e2e: A blocks full brief, B blocks e5 brief, -ub 512 all blocks, receipt arm_suite recorded")


if __name__ == "__main__":
    if "-m" in sys.argv[1:]:
        fake_server(sys.argv[1:])
    else:
        main()

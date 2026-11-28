#!/usr/bin/env python3
"""C56-brief-odd dry check (no model, no network): the `lanespeed.py --offset` case selection.

  python3 lanespeed_c56brief_odd_check.py        # exit 0 = all checks pass

Runs lanespeed.py end to end against the fake llama-server in lanespeed_c56brief_check.py, twice:
1. default (no --offset), --every 2: the 15 even-index cases exactly as C56 / C56-brief scored them, and the receipt
   has no "offset" key (the default receipt keys are unchanged).
2. --every 2 --offset 1: the 15 odd-index cases, in suite order, 15 rows per block, receipt "offset": 1, and the
   frozen lanespeed_verdict.py accepts the receipt.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVEN = ["f-capital", "f-light", "f-austen", "r-apples", "r-order", "r-percent", "c-dentist", "c-budget", "c-grok",
        "u-fahrenheit", "u-wait", "s-screen", "s-spotify", "s-build", "s-email"]  # C56-brief run 45767553751 case_ids
ODD = ["f-everest", "f-planet", "f-gold", "r-mult", "r-train", "r-days", "c-maya", "c-engine", "c-ram",
       "u-australia", "u-correct", "s-text", "s-files", "s-mom", "s-weather"]


def run(td, name, extra):
    wrapper = Path(td) / "llama-server"
    if not wrapper.exists():
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "lanespeed_c56brief_check.py"}" "$@"\n')
        wrapper.chmod(0o755)
    logdir = Path(td) / name
    logdir.mkdir()
    out = logdir / "r.json"
    subprocess.run([sys.executable, str(HERE / "lanespeed.py"), "--gguf", str(HERE / "slm-v2.json"),
                    "--blocks", "A:512,B:512,A:512,B:512", "--every", "2", "--deadline-s", "600",
                    "--suite-b", str(HERE / "slm-v2-e5.json"), "--out", str(out), "--logdir", str(logdir), *extra],
                   env={**os.environ, "LLAMA_SERVER": str(wrapper)}, check=True)
    return out, json.loads(out.read_text())


def main():
    suite = json.loads((HERE / "slm-v2.json").read_text())
    ids = [c["id"] for c in suite["cases"]]
    assert ids[0::2] == EVEN and ids[1::2] == ODD, "suite order changed"
    with tempfile.TemporaryDirectory() as td:
        _, even = run(td, "even", [])
        assert even["complete"] and even["case_ids"] == EVEN and "offset" not in even
        assert [[r["case"] for r in b["rows"]] for b in even["blocks"]] == [EVEN] * 4
        print("ok default: 15 even-index cases, no offset key")

        out, odd = run(td, "odd", ["--offset", "1"])
        assert odd["complete"] and odd["case_ids"] == ODD and odd["offset"] == 1
        assert [b["label"] for b in odd["blocks"]] == ["A1", "B1", "A2", "B2"]
        assert [[r["case"] for r in b["rows"]] for b in odd["blocks"]] == [ODD] * 4
        assert all(b["ub"] == 512 and b["summary"]["n"] == 15 and not b["summary"]["errors"] for b in odd["blocks"])
        assert set(odd) - set(even) == {"offset"} and set(even) <= set(odd)
        verdict = subprocess.run([sys.executable, str(HERE / "lanespeed_verdict.py"), str(out)], check=True,
                                 capture_output=True, text=True).stdout
        v = json.loads(verdict)
        assert v["verdict"] in ("KEEP", "NO_GAIN", "REVERT") and v["n_cases"] == 15
        print("ok --offset 1: 15 odd-index cases per block, receipt offset 1, frozen verdict reads it")


if __name__ == "__main__":
    main()

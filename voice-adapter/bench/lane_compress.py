"""Derive a lane suite whose brief is compressed to a phone-lane budget (~300 tokens), cases unchanged.

A phone lane pays ~1.7 ms per cached token on every warm turn (B11), so it needs a short context. Two ways to get
one from the chat tail, both local only (chat text never leaves the Studio):
  tail   keep the newest CHARS characters (what snapshot() does today, at a smaller cap)
  model  a local GGUF rewrites the tail as the owner's personal facts, plans and settings in <= CHARS characters

  .venv/bin/python bench/lane_compress.py --suite bench/slm-v2.json --mode model --model qwen3.5-9b > /tmp/v2c.json
Then score the derived suite with slm_bench.py --suite. The compressed brief and its timing go in "compressed".
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import slm_bench  # noqa: E402

PROMPT = ("Below is the tail of a chat between the owner and his assistant. Write the facts a voice assistant would "
          "need to answer the owner's questions about his own life and setup: appointments, people, flights, budgets, "
          "decisions, settings, numbers, permissions and status lines. Keep every number, time, name and place "
          "exactly. Drop general-knowledge answers, recipes, advice and explanations entirely. One fact per line, "
          "no bullets or headings, at most {chars} characters in total.")


def compress(text, mode, chars, model):
    if mode == "tail":
        return text[-chars:], None
    proc = slm_bench.serve(slm_bench.MODELS / f"{model}.gguf")
    try:
        t0 = time.monotonic()
        # not ask_local: it caps replies at 120 tokens and a ~300-token brief needs more
        body = {"messages": [{"role": "system", "content": PROMPT.format(chars=chars)}, {"role": "user", "content": text}],
                "temperature": 0, "max_tokens": 600, "chat_template_kwargs": {"enable_thinking": False}}
        with slm_bench.post(f"http://127.0.0.1:{slm_bench.PORT}/v1/chat/completions", body) as r:
            reply = json.load(r)["choices"][0]["message"]["content"]
        return slm_bench.THINK.sub("", reply).strip(), round(time.monotonic() - t0, 2)
    finally:
        slm_bench.stop(proc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True)
    ap.add_argument("--mode", choices=["tail", "model"], required=True)
    ap.add_argument("--chars", type=int, default=1100)
    ap.add_argument("--model", default="qwen3.5-9b")
    o = ap.parse_args()
    suite = json.loads(Path(o.suite).read_text())
    brief, seconds = compress(suite["brief"], o.mode, o.chars, o.model)
    suite.update(suite=f"{suite['suite']}-{o.mode}{o.chars}", brief=brief,
                 compressed={"from": o.suite, "mode": o.mode, "chars": o.chars, "out_chars": len(brief),
                             "model": o.model if o.mode == "model" else None, "seconds": seconds})
    print(json.dumps(suite, indent=1))


if __name__ == "__main__":
    main()

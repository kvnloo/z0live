#!/bin/bash
# Run on the desktop host: lane-latency probe for one phone (Track B harness).
# llama-server on the phone, one cold request carrying a ~1k-token call brief, then 5 turns that each append ~50
# new tokens (previous reply + a new user line). Metric: median time to first token of the 5 warm turns, which is
# the real lane cost when the brief stays in the KV cache. A final turn with cache_prompt=false is the no-cache
# control in the same harness.
# Usage: phone_lane_probe.sh SERIAL MODEL [set=generic|android|vulkan] [extra llama-server args...]
#   set=vulkan pushes the static Vulkan llama-server built with bench/ggml-vulkan-mali-vk11.patch (B14).
#   BRIEF_FILE=path (on the desktop host): lane mode. The file is the exact lane system text (tiers.SYSTEM + tiers.snapshot(brief, []),
#   written by the caller); each turn is a stateless lane request [system + "This call so far", ask] rendered by
#   the model's own chat template (/apply-template), as tiers.race sends it. Unset: the frozen ChatML brief.
set -u
S=$1 G=$2 SET=${3:-generic}; shift 3 2>/dev/null || shift $#
M=${PHONE_SLM_MODELS:?set PHONE_SLM_MODELS to the dir holding android/ builds and GGUFs}
OUT=${OUT:-$HOME/.local/share/z0live/phone-lane-$(date +%Y%m%d-%H%M)}
D=/data/local/tmp/z0lane PORT=18080
mkdir -p "$OUT"
a() { adb -s "$S" "$@"; }
a shell "mkdir -p $D" >/dev/null
case $SET in
  generic) src=$M/android-generic/llama-b12527; a push "$src"/*.so "$src/llama-server" "$D/" >/dev/null ;;
  vulkan) a push "$M/android-vulkan-bda/llama-server" "$D/llama-server" >/dev/null ;;
  *) src=$M/android/llama-b12527; a push "$src"/lib/*.so "$src/bin/llama-server" "$D/" >/dev/null ;;
esac
want=$(stat -c %s "$M/$G.gguf"); have=$(a shell "stat -c %s $D/$G.gguf 2>/dev/null" | tr -d '\r')
[ "$want" = "$have" ] || { a shell "rm -f $D/*.gguf"; a push "$M/$G.gguf" "$D/$G.gguf" >/dev/null || { echo "push failed"; exit 1; }; }
a shell chmod 755 "$D/llama-server"
JINJA=; [ -n "${BRIEF_FILE:-}" ] && JINJA=--jinja
# setsid: an adbd restart (the tab's wireless adb drops under load) kills every process left in its session.
a shell "cd $D && (LD_LIBRARY_PATH=$D ADSP_LIBRARY_PATH=$D setsid ./llama-server -m $G.gguf --host 127.0.0.1 \
  --port 8080 -c 4096 -np 1 -t 4 $JINJA $* > $D/server.log 2>&1 < /dev/null & echo \$! > $D/server.pid)"
# The tab's wireless adb drops for a few seconds under load: retry the forward until the server answers.
for i in $(seq 120); do
  curl -sf localhost:$PORT/health >/dev/null && break
  a forward tcp:$PORT tcp:8080 >/dev/null 2>&1; sleep 1
done
python3 - "$OUT/$G.json" "$PORT" <<'EOF'
import json, os, statistics, sys, time, urllib.request
out, port = sys.argv[1], sys.argv[2]
U = f"http://127.0.0.1:{port}"
facts = ["The user is building z0live, a voice layer over Hermes with fast and slow model lanes.",
         "The open chat is about overnight hill-climbs on phone lanes and Dessa-level turn timing.",
         "The S25 Ultra reaches about one second to first word on its GPU with Gemma 4 E2B.",
         "The tablet runs CPU only and needs about thirty seconds for a one thousand token prompt.",
         "Lanes are stateless; one context snapshot of brief plus recent turns goes to every lane.",
         "A lane answers in one or two short spoken sentences, or says SKIP when it cannot help."]
brief = "<|im_start|>system\nYou are a fast voice lane for the user. Call brief follows.\n"
i = 0
while len(brief) < int(os.environ.get("BRIEF_CHARS", "4300")):
    brief += f"- ({i}) " + facts[i % len(facts)] + "\n"; i += 1
brief += "<|im_end|>\n"
users = ["What did the phone bench say about the S25?", "And the tablet, is it usable at all?",
         "Why is the prefix cache supposed to help?", "Which model should the phone lane run first?",
         "Okay, what should I test tomorrow morning?", "Summarize that in one sentence."]
def post(path, body):
    r = urllib.request.Request(U + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=600)
def turn(prompt, cache=True):
    t0 = time.monotonic(); first = None; text = ""; timings = {}
    with post("/completion", {"prompt": prompt, "n_predict": 24, "stream": True, "cache_prompt": cache,
                              "temperature": 0, "id_slot": 0}) as r:
        for line in r:
            if not line.startswith(b"data: "): continue
            c = json.loads(line[6:])
            if c.get("content") and first is None: first = time.monotonic() - t0
            text += c.get("content", "")
            if c.get("stop"): timings = c.get("timings", {})
    return {"ttft_s": round(first or (time.monotonic() - t0), 3), "total_s": round(time.monotonic() - t0, 3),
            "prompt_n": timings.get("prompt_n"), "cache_n": timings.get("cache_n"),
            "prompt_ms": timings.get("prompt_ms"), "text": text}
def ntok(text):
    return len(json.load(post("/tokenize", {"content": text}))["tokens"])
rows = []
if os.environ.get("BRIEF_FILE"):
    system = open(os.environ["BRIEF_FILE"]).read()
    def lane(exchanges, ask):  # tiers.snapshot's call-so-far block, then the ask as the user message
        calls = "This call so far:\n" + "\n".join(f"{w}: {t}" for w, t in exchanges) if exchanges else ""
        msgs = [{"role": "system", "content": system + calls}, {"role": "user", "content": ask}]
        return json.load(post("/apply-template", {"messages": msgs,
                                                  "chat_template_kwargs": {"enable_thinking": False}}))["prompt"]
    n_brief, exchanges = ntok(lane([], "")), []
    for k, u in enumerate(users):
        r = turn(lane(exchanges, u)); r["turn"] = k; rows.append(r); exchanges += [("The user", u), ("You", r["text"].strip())]
        print(f"turn {k} ttft {r['ttft_s']:6.2f}s prompt_n {r['prompt_n']} cache_n {r['cache_n']}", flush=True)
    ctl = turn(lane(exchanges, "Thanks."), cache=False)
else:
    n_brief = ntok(brief)
    conv = brief
    for k, u in enumerate(users):
        conv += f"<|im_start|>user\n{u}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        r = turn(conv); r["turn"] = k; rows.append(r); conv += r["text"] + "<|im_end|>\n"
        print(f"turn {k} ttft {r['ttft_s']:6.2f}s prompt_n {r['prompt_n']} cache_n {r['cache_n']}", flush=True)
    ctl = turn(conv + "<|im_start|>user\nThanks.<|im_end|>\n<|im_start|>assistant\n", cache=False)
warm = [r["ttft_s"] for r in rows[1:]]
res = {"brief_tokens": n_brief, "cold_ttft_s": rows[0]["ttft_s"], "warm_ttft_s": warm,
       "warm_median_s": statistics.median(warm), "nocache_control_ttft_s": ctl["ttft_s"],
       "nocache_control_prompt_n": ctl["prompt_n"], "rows": rows, "control": ctl}
json.dump(res, open(out, "w"), indent=1)
print(f"brief {n_brief} tok | cold {rows[0]['ttft_s']:.2f}s | warm median {res['warm_median_s']:.2f}s "
      f"{warm} | no-cache control {ctl['ttft_s']:.2f}s ({ctl['prompt_n']} tok)")
EOF
# Kill only this probe's server: other llama-servers on the phone (hre-slm on :8081) belong to other work.
# The model stays in $D so the next probe skips the push; KEEP_MODEL=0 removes it.
a shell "kill \$(cat $D/server.pid)"; [ "${KEEP_MODEL:-1}" = 1 ] || a shell "rm -rf $D"; a forward --remove tcp:$PORT
a shell "dumpsys thermalservice | grep -m1 'Thermal Status'"
echo "receipts: $OUT"

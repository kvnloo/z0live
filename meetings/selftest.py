"""hermes-meetings M1 self-test, no human needed.

  1. macOS `say` speaks the questions to WAV (synthetic voices).
  2. A loopback auth stub stands in for the Hermes dashboard's /api/auth/me (the only stubbed piece of the voice path).
  3. A dev copy of the z0live voice adapter (a git worktree of the deployed release, own port, own trace dir, chat
     continuity / handoff / lanes / passthru off) serves /api/audio/voice-live/session exactly as for the phone.
  4. meetings.server.Meetings starts livekit-server (loopback config) and the room bot (meetings_start path).
  5. In the room: `guest-ana` (full role) asks a question first -> must stay context only (no reply, 0 frames
     forwarded); then the owner `kevin` asks -> the bot's published audio is recorded by `guest-rec` (view role, which
     must fail to publish), transcribed locally (LFM2.5-Audio, CPU) and checked against an answer regex.
  6. Latency: end of the owner's question (as played into the room) -> first loud frame of z0's room track, one clock.
  7. Everything is stopped; results go to <run>/result.json.

usage: python selftest.py --run DIR --adapter-dir VOICE_ADAPTER_DIR --adapter-python PY [--profile tek]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import av
import numpy as np
from aiohttp import web
from livekit import rtc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import Meetings  # noqa: E402

SR = 48000
LOUD = 500
SAY = "/usr/bin/say"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
LFM = Path.home() / ".local/share/z0live/models/lfm25-audio"
LFM_RUNNER = LFM / "llama-liquid-audio-macos-arm64/llama-liquid-audio-macos-arm64/llama-liquid-audio-server"
LIQUID = "LFM2.5-Audio-1.5B-Q4_0.gguf"
T0 = time.monotonic()


def log(event, **kw):
    print(json.dumps({"t": round(time.monotonic() - T0, 3), "event": event, **kw}, ensure_ascii=False), flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tts(text, voice, out: Path) -> np.ndarray:
    aiff = out.with_suffix(".aiff")
    subprocess.run([SAY, "-v", voice, "-o", str(aiff), text], check=True, timeout=60)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(aiff), "-ac", "1", "-ar", str(SR), "-sample_fmt", "s16",
                    str(out)], check=True, timeout=60)
    aiff.unlink()
    with wave.open(str(out)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def spurts(pcm: np.ndarray, gap_s=1.0, pad_s=0.2, max_s=25.0):
    """Loud segments of a recording (energy per 10 ms), so ASR never sees long silence (LFM invents text there)."""
    n = 480
    loud = [int(np.abs(pcm[i:i + n].astype(np.int32)).max()) >= LOUD for i in range(0, len(pcm) - n + 1, n)]
    out, start, last = [], None, None
    for k, on in enumerate(loud):
        if on:
            start = k if start is None else start
            last = k
        if start is not None and (k - last) * n / SR > gap_s or (start is not None and (k - start) * n / SR > max_s):
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    pad = int(pad_s * SR / n)
    return [pcm[max(0, s - pad) * n:(e + 1 + pad) * n] for s, e in out]


def write_wav(path: Path, pcm: np.ndarray, rate=SR):
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(pcm.astype("<i2").tobytes())


def to16k(pcm48: np.ndarray) -> np.ndarray:
    rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
    fr = av.AudioFrame.from_ndarray(pcm48.reshape(1, -1), format="s16", layout="mono")
    fr.sample_rate = SR
    out = [o.to_ndarray().reshape(-1) for o in rs.resample(fr)] + [o.to_ndarray().reshape(-1) for o in rs.resample(None)]
    return np.concatenate(out) if out else np.zeros(0, np.int16)


# -- local ASR (LFM2.5-Audio, CPU, loopback) -----------------------------------------------------------------------
class ASR:
    def __init__(self, logdir: Path):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.logf = logdir / "asr-server.log"
        args = [LFM_RUNNER, "--host", "127.0.0.1", "--port", str(self.port), "--offline", "-c", "4096", "-ngl", "0",
                "-t", "6", "-s", "42", "-m", LFM / LIQUID, "-mm", LFM / f"mmproj-{LIQUID}",
                "-mv", LFM / f"vocoder-{LIQUID}", "--tts-speaker-file", LFM / f"tokenizer-{LIQUID}"]
        self.stream = self.logf.open("wb")
        self.proc = subprocess.Popen([str(x) for x in args], stdout=self.stream, stderr=self.stream,
                                     cwd=LFM_RUNNER.parent, start_new_session=True)

    def wait(self, timeout=180):
        deadline = time.time() + timeout
        while b"Server ready at" not in self.logf.read_bytes():
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f"ASR server did not start; see {self.logf}")
            time.sleep(0.3)

    def __call__(self, pcm16k: np.ndarray) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            w.writeframes(pcm16k.astype("<i2").tobytes())
        body = {"messages": [{"role": "system", "content": "Perform ASR."},
                             {"role": "user", "content": [{"type": "input_audio", "input_audio": {
                                 "format": "wav", "data": base64.b64encode(buf.getvalue()).decode()}}]}],
                "stream": True, "max_tokens": 400, "reset_context": True}
        req = urllib.request.Request(self.url + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        out = []
        with urllib.request.urlopen(req, timeout=180) as r:
            for line in r:
                data = line[5:].strip() if line.startswith(b"data:") else b""
                if data and data != b"[DONE]":
                    out.append(json.loads(data)["choices"][0]["delta"].get("content") or "")
        return "".join(out).strip()

    def close(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


# -- auth stub: stands in for the dashboard's /api/auth/me ---------------------------------------------------------
async def auth_stub(token: str):
    async def me(request):
        if request.headers.get("Authorization", "") != "Bearer " + token:
            return web.json_response({"authenticated": False}, status=401)
        return web.json_response({"authenticated": True, "user": "hermes-meetings-selftest"})
    app = web.Application()
    app.router.add_get("/api/auth/me", me)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def start_adapter(a, run: Path, upstream_port: int) -> tuple[subprocess.Popen, int]:
    port = free_port()
    d = run / "adapter"
    d.mkdir(exist_ok=True)
    # the adapter runs like its launchd unit: no inherited kanban/session/profile context from this test's parent
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("HERMES_KANBAN", "HERMES_SESSION", "HERMES_TENANT")) and k != "HERMES_HOME"}
    env = {**base,
           "HERMES_MOBILE_VOICE_PORT": str(port),
           "HERMES_DASHBOARD_UPSTREAM": f"127.0.0.1:{upstream_port}",
           "HERMES_MOBILE_VOICE_TRACE_DIR": str(d / "traces"),
           "HERMES_VOICE_BRIEF_DIR": str(d / "briefs"),
           "Z0LIVE_HANDOFF_FILE": str(d / "handoff.json"), "HERMES_VOICE_HANDOFF": "off",
           "HERMES_MOBILE_VOICE_CHAT": "0",            # never write voice turns into Kevin's open chat
           "HERMES_MOBILE_VOICE_LANES": "0", "Z0LIVE_TIERS": "off",  # live lanes/TV untouched (M1 gap, see REPORT)
           "Z0LIVE_PASSTHRU": "off", "Z0LIVE_NEEDS_VOICE": "0", "Z0LIVE_GUEST": "off",
           "Z0LIVE_VOICES_FILE": str(d / "voices.json"),
           "Z0LIVE_LEDGER": str(d / "ledger"), "Z0LIVE_NEEDS_MEMORY": str(d / "needs.json"),
           "Z0LIVE_CONDUCTOR": str(d / "conductor"), "Z0LIVE_TV_URL": "http://127.0.0.1:9",
           "Z0LIVE_DESKTOP": str(Path(a.adapter_dir).parent / "desktop"),
           "HERMES_VOICE_ENGINE": "gpt-live",
           "HERMES_BIN": a.hermes_bin}
    (d / "voices.json").write_text('{"guest": ""}')
    logf = (d / "adapter.log").open("ab")
    proc = subprocess.Popen([a.adapter_python, "mobile_live.py"], cwd=a.adapter_dir, env=env, stdout=logf,
                            stderr=logf, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"adapter exited {proc.returncode}; see {d / 'adapter.log'}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if json.loads(r.read()).get("ready"):
                    return proc, port
        except OSError:
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError("adapter did not become ready")


def adapter_events(run: Path, names) -> list:
    out = []
    for f in sorted((run / "adapter" / "traces").glob("*/*.jsonl")):
        for line in f.read_text(errors="ignore").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ev") in names:
                out.append({k: (str(v)[:300] if isinstance(v, str) else v) for k, v in e.items()
                            if k in ("ev", "t", "request", "answer", "seconds", "error", "kind", "text")})
    return out


# -- room participants ---------------------------------------------------------------------------------------------
class Speaker:
    def __init__(self, name):
        self.name = name
        self.room = rtc.Room()
        self.src = rtc.AudioSource(SR, 1, queue_size_ms=40)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ended = {}

    async def join(self, url, token):
        t0 = time.monotonic()
        await self.room.connect(url, token)
        track = rtc.LocalAudioTrack.create_audio_track(self.name + "-mic", self.src)
        await self.room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        log("joined", who=self.name, join_ms=round((time.monotonic() - t0) * 1000))
        asyncio.ensure_future(self.loop())

    async def loop(self):
        """Continuous mic: silence, or the queued utterance; marks when its last sample has played into the room."""
        while True:
            try:
                key, pcm = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                key, pcm = None, np.zeros(480, np.int16)
            for i in range(0, len(pcm), 480):
                chunk = pcm[i:i + 480]
                if chunk.size < 480:
                    chunk = np.concatenate([chunk, np.zeros(480 - chunk.size, np.int16)])
                f = rtc.AudioFrame.create(SR, 1, 480)
                np.copyto(np.frombuffer(f.data, dtype=np.int16), chunk)
                await self.src.capture_frame(f)
            if key:
                self.ended[key] = time.monotonic() + self.src.queued_duration
                log("utterance.end", who=self.name, key=key)

    async def say(self, key, pcm):
        await self.queue.put((key, pcm))
        while key not in self.ended:
            await asyncio.sleep(0.01)
        return self.ended[key]


class Recorder:
    def __init__(self):
        self.room = rtc.Room()
        self.frames = []  # (monotonic arrival, pcm)
        self.chat = []
        self.publish_denied = None

    async def join(self, url, token):
        @self.room.on("track_subscribed")
        def sub(track, pub, p):
            if p.identity == "z0" and track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.ensure_future(self.rec(track))

        def chat(reader, who):
            async def read():
                text = await reader.read_all()
                self.chat.append({"t": round(time.monotonic() - T0, 3), "from": who, "text": text})
            asyncio.ensure_future(read())
        self.room.register_text_stream_handler("lk.chat", chat)
        await self.room.connect(url, token)
        try:  # view role: publishing must be refused by the server
            src = rtc.AudioSource(SR, 1)
            await asyncio.wait_for(self.room.local_participant.publish_track(
                rtc.LocalAudioTrack.create_audio_track("nope", src)), 8)
            self.publish_denied = False
        except Exception as exc:  # noqa: BLE001
            self.publish_denied = f"{type(exc).__name__}: {str(exc)[:120]}"

    async def rec(self, track):
        log("recording", track=track.sid)
        async for ev in rtc.AudioStream(track, sample_rate=SR, num_channels=1):
            self.frames.append((time.monotonic(), np.frombuffer(ev.frame.data, dtype=np.int16).copy()))

    def first_loud_after(self, t):
        for at, pcm in self.frames:
            if at >= t and int(np.abs(pcm.astype(np.int32)).max()) >= LOUD:
                return at
        return None

    def loud_frames(self, t0, t1):
        return sum(1 for at, pcm in self.frames if t0 <= at < t1 and int(np.abs(pcm.astype(np.int32)).max()) >= LOUD)

    def audio(self, t0, t1):
        parts = [pcm for at, pcm in self.frames if t0 <= at < t1]
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)


async def wait_for(pred, timeout, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


async def main(a):
    run = Path(a.run)
    run.mkdir(parents=True, exist_ok=True)
    res = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": {}}
    q_owner = a.owner_question
    q_guest = "Hey z0, what is the capital of Japan? Please answer me."
    log("tts")
    owner_pcm = tts(q_owner, "Samantha", run / "q_owner.wav")
    guest_pcm = tts(q_guest, "Daniel", run / "q_guest.wav")
    asr = ASR(run)
    bearer = secrets.token_urlsafe(24)
    (run / "voice.bearer").write_text(bearer)
    os.chmod(run / "voice.bearer", 0o600)
    stub, stub_port = await auth_stub(bearer)
    adapter = None
    m = Meetings(run / "meeting", bot_python=sys.executable)
    speakers = []
    rec = Recorder()
    try:
        adapter, adapter_port = await asyncio.to_thread(start_adapter, a, run, stub_port)
        await asyncio.to_thread(asr.wait)
        log("adapter.ready", port=adapter_port)
        started = m.start(mode="loopback", room="m1-selftest", port=a.lk_port, udp_port=a.lk_udp, bot=True,
                          owner="kevin", bot_args=["--voice-url", f"http://127.0.0.1:{adapter_port}",
                                                   "--voice-bearer-file", str(run / "voice.bearer"),
                                                   "--profile", a.profile, "--guest-asr-url", asr.url])
        res["meetings_start"] = started
        log("meetings_start", **started)
        if not await wait_for(lambda: any(p["identity"] == "z0" for p in m.participants()), 30):
            raise RuntimeError("bot never joined")
        inv_owner = m.invite(role="owner")
        inv_guest = m.invite("ana", role="full")
        inv_rec = m.invite("rec", role="view")
        res["invites"] = {k: {"identity": v["identity"], "role": v["role"], "link_prefix": (v["link"] or "").split("&token=")[0]}
                          for k, v in (("owner", inv_owner), ("guest", inv_guest), ("recorder", inv_rec))}
        await rec.join(started["url"], inv_rec["token"])
        res["checks"]["view_role_publish_denied"] = rec.publish_denied
        kevin, ana = Speaker("kevin"), Speaker("guest-ana")
        speakers = [kevin, ana]
        await ana.join(started["url"], inv_guest["token"])
        await kevin.join(started["url"], inv_owner["token"])
        # the bot opens the voice leg when the owner's track arrives
        await wait_for(lambda: (m.status().get("bot") or {}).get("voice", {}).get("session"), 60, 0.5)
        st = m.status()
        res["status_after_join"] = {k: st.get(k) for k in ("participants", "listeners", "running", "bot_running")}
        res["status_after_join"]["voice"] = (st.get("bot") or {}).get("voice")
        log("voice.session", voice=res["status_after_join"]["voice"])
        await asyncio.sleep(3)
        # --- guest speaks first: must stay context only
        g_end = await ana.say("guest_q", guest_pcm)
        await asyncio.sleep(10)
        res["checks"]["guest_loud_reply_frames_10s"] = rec.loud_frames(g_end - len(guest_pcm) / SR, time.monotonic())
        # --- owner asks
        k_end = await kevin.say("owner_q", owner_pcm)
        res["owner_speech_end_t"] = round(k_end - T0, 3)
        got = await wait_for(lambda: rec.first_loud_after(k_end) is not None, 45, 0.05)
        first = rec.first_loud_after(k_end)
        res["latency_s"] = {"speech_end_to_first_reply_audio": round(first - k_end, 3) if first else None}
        rx = re.compile(a.answer_regex, re.I)
        if got and a.wait_s:  # a delegated answer: keep recording until the room chat carries it, or wait_s
            await wait_for(lambda: any(rx.search(c["text"]) for c in rec.chat), a.wait_s, 0.5)
            res["chat_match_t"] = next((c["t"] for c in rec.chat if rx.search(c["text"])), None)
        # let the reply finish: 2.5 s with no loud frames, max 40 s
        if got:
            await wait_for(lambda: rec.frames and time.monotonic() - max(
                [at for at, pcm in rec.frames if int(np.abs(pcm.astype(np.int32)).max()) >= LOUD] or [0]) > 2.5, 40, 0.2)
        if res.get("chat_match_t"):
            res["latency_s"]["speech_end_to_answer_in_chat"] = round(res["chat_match_t"] - (k_end - T0), 3)
        reply = rec.audio(k_end, time.monotonic())
        write_wav(run / "reply_room_recording.wav", reply)
        write_wav(run / "guest_window_recording.wav", rec.audio(g_end - len(guest_pcm) / SR, k_end - len(owner_pcm) / SR))
        res["reply_seconds_recorded"] = round(len(reply) / SR, 2)
        st = m.status()
        bot = st.get("bot") or {}
        res["bot_status"] = {k: bot.get(k) for k in ("forwarded_to_voice", "frames_in", "talkspurts", "counts", "voice")}
        res["bot_transcript"] = bot.get("transcript")
        res["room_chat"] = rec.chat
        # --- local ASR
        await asyncio.to_thread(asr.wait)
        res["asr_question_check"] = await asyncio.to_thread(asr, to16k(owner_pcm))
        segs = spurts(reply)
        res["asr_reply_segments"] = [await asyncio.to_thread(asr, to16k(s)) for s in segs]
        res["asr_reply"] = " | ".join(res["asr_reply_segments"])
        guest_win = rec.audio(g_end - len(guest_pcm) / SR, k_end - len(owner_pcm) / SR)
        res["guest_window_loud_frames"] = rec.loud_frames(g_end - len(guest_pcm) / SR, k_end - len(owner_pcm) / SR)
        res["adapter_delegations"] = adapter_events(run, ("delegation.start", "delegation.hermes", "delegation.done",
                                                          "delegation.failed", "delegation.receipt",
                                                          "delegation.refused", "delegation.ttft"))
        c = res["checks"]
        c["reply_audio_heard"] = first is not None
        c["asr_reply_matches"] = bool(rx.search(res["asr_reply"] or ""))
        c["guest_never_forwarded"] = (bot.get("forwarded_to_voice") or {}).get("guest-ana", 0) == 0 and \
            (bot.get("frames_in") or {}).get("guest-ana", 0) > 0
        c["guest_transcribed_locally"] = any(x["who"] == "guest-ana" and x.get("heard_by") == "local-context"
                                             and re.search(r"capital of japan", x["text"], re.I)
                                             for x in (res["bot_transcript"] or []))
        c["owner_forwarded_frames"] = (bot.get("forwarded_to_voice") or {}).get("kevin", 0)
        c["no_reply_to_guest"] = res["guest_window_loud_frames"] == 0
        c["guest_answer_absent_from_reply"] = not re.search(r"tokyo", res["asr_reply"] or "", re.I)
        c["loopback_only"] = all(("127.0.0.1" in x) for x in (res["status_after_join"].get("listeners") or ["none"]))
        c["view_role_publish_denied"] = bool(rec.publish_denied)
        res["pass"] = all([c["reply_audio_heard"], c["asr_reply_matches"], c["guest_never_forwarded"],
                           c["no_reply_to_guest"], c["loopback_only"], c["view_role_publish_denied"],
                           c["guest_transcribed_locally"], c["owner_forwarded_frames"] > 0,
                           c["guest_answer_absent_from_reply"]])
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
        res["pass"] = False
    finally:
        for s in speakers:
            await s.room.disconnect()
        await rec.room.disconnect()
        res["meetings_leave"] = m.leave()
        if adapter and adapter.poll() is None:
            adapter.send_signal(signal.SIGTERM)
            try:
                adapter.wait(15)
            except subprocess.TimeoutExpired:
                adapter.kill()
        asr.close()
        await stub.cleanup()
        (run / "result.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
        log("done", passed=res["pass"])
        print(json.dumps({k: res.get(k) for k in ("pass", "checks", "latency_s", "asr_reply", "error")}, indent=1,
                         ensure_ascii=False))
    return res["pass"]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--adapter-dir", required=True)
    p.add_argument("--adapter-python", required=True)
    p.add_argument("--hermes-bin", default=str(Path.home() / ".hermes/hermes-agent/.hermes/bin/hermes"))
    p.add_argument("--profile", default="tek")
    p.add_argument("--owner-question", default="What is the capital of France? Answer in one short sentence.")
    p.add_argument("--answer-regex", default=r"\bparis\b")
    p.add_argument("--wait-s", type=float, default=0, help="delegation: record up to this long for the answer")
    p.add_argument("--lk-port", type=int, default=7890)
    p.add_argument("--lk-udp", type=int, default=7892)
    sys.exit(0 if asyncio.run(main(p.parse_args())) else 1)

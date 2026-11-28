"""End-to-end passthru check against REAL Google Meet: z0 joins a meeting that an agent browser hosts.

Both sides are agent-owned; no owner screen, phone or other people are involved.
  HOST = a signed-in Chrome on a private display, reachable over CDP (Z0_MEET_HOST_CDP, default
         http://127.0.0.1:19377, e.g. `ssh -N -L 19377:127.0.0.1:9377 groot`). Its fake mic plays a
         known phrase on loop (Chrome --use-file-for-fake-audio-capture). testroom/meet_host.mjs drives it.
  z0   = the production meeting leg (Passthru + meeting_leg.mjs + recipes/google-meet.mjs), headless.

Steps: host starts meet.google.com/new -> z0 asks to join -> host admits -> z0 counts room audio
frames (host's phrase) -> z0 speaks a TTS phrase through Passthru.voice() while the host records the
remote audio -> both recordings are transcribed (optional) -> z0 and host leave.

Pass = z0 admitted, room frames arrive with speech level, the host's recording carries z0's speech
(peak RMS), and, when an STT python is configured (Z0_MEET_STT_PY with mlx_audio), the host-side
transcript contains the phrase. Artifacts go to --out (default ./e2e-meet-<ts>/).
Run with the voice-adapter venv:  .venv/bin/python passthru/e2e_meet.py [--out DIR] [--phrase TEXT]
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import av
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import passthru  # noqa: E402

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")


class FakeCall:
    id = "e2e-meet-" + str(int(time.time()))

    def __init__(self, log):
        self.events, self.tasks, self.log = [], set(), log

    def trace(self, ev, **data):
        self.events.append((time.time(), ev, data))
        self.log(ev, data, quiet=ev == "passthru.leg.stats")

    def spawn(self, coro):
        t = asyncio.ensure_future(coro); self.tasks.add(t); t.add_done_callback(self.tasks.discard); return t


def redact(s: str) -> str:
    return re.sub(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", "meet.google.com/<code>", s)


def frame(pcm: np.ndarray, pts: int):
    f = av.AudioFrame.from_ndarray(np.repeat(pcm, 2).reshape(1, -1), format="s16", layout="stereo")
    f.sample_rate, f.pts = 48000, pts
    return f


def tts_wav(text: str, path: Path) -> np.ndarray:
    """z0's line as 48 kHz int16 mono (macOS `say`; set Z0_MEET_TTS_WAV to use a ready file)."""
    src = os.environ.get("Z0_MEET_TTS_WAV")
    if not src:
        subprocess.run(["say", "-o", str(path), "--data-format=LEI16@48000", text], check=True)
        src = str(path)
    with wave.open(src) as w:
        assert w.getframerate() == 48000 and w.getsampwidth() == 2, "need 48 kHz s16 wav"
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return a.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)


def transcribe(audio: Path) -> str:
    py = os.environ.get("Z0_MEET_STT_PY")
    if not py:
        return ""
    base = audio.with_suffix("")
    model = ["--model", os.environ["Z0_MEET_STT_MODEL"]] if os.environ.get("Z0_MEET_STT_MODEL") else []
    r = subprocess.run([py, "-m", "mlx_audio.stt.generate", *model, "--audio", str(audio), "--output-path", str(base),
                        "--format", "txt"], capture_output=True, timeout=600)
    if r.returncode:
        return "<stt failed: " + r.stderr.decode(errors="replace").strip().splitlines()[-1][:160] + ">" if r.stderr else "<stt failed>"
    for p in (Path(str(base) + ".txt"), base / (audio.stem + ".txt")):
        if p.exists():
            return p.read_text().strip()
    return ""


Z0_RTP_JS = """(async () => { const o = { in_bytes: 0, in_energy: 0, out_bytes: 0, pcs: 0 };
  for (const pc of (window.__z0passthru ? window.__z0passthru.pcs : [])) { if (pc.connectionState === 'closed') continue; o.pcs++;
    (await pc.getStats()).forEach(s => { if (s.kind !== 'audio') return;
      if (s.type === 'inbound-rtp') { o.in_bytes += s.bytesReceived || 0; o.in_energy += s.totalAudioEnergy || 0; }
      if (s.type === 'outbound-rtp') o.out_bytes += s.bytesSent || 0;
      if (s.type === 'media-source') o.out_energy = (o.out_energy || 0) + (s.totalAudioEnergy || 0); });
    for (const r of pc.getReceivers()) if (r.track && r.track.kind === 'audio')
      for (const s of r.getSynchronizationSources()) o.in_level_max = Math.max(o.in_level_max || 0, s.audioLevel || 0);
    for (const s of pc.getSenders()) if (s.track && s.track.kind === 'audio') { o.mic_enabled = s.track.enabled; o.mic_muted = s.track.muted; o.mic_label = s.track.label; } }
  return JSON.stringify(o); })()"""


async def z0_rtp(p, call) -> dict:
    """Audio RTP totals in z0's meeting page (needs Z0LIVE_PASSTHRU_DEBUG for the leg's eval command)."""
    n = len(call.events)
    await p.send("eval", js=Z0_RTP_JS)
    for _ in range(50):
        for _t, ev, data in call.events[n:]:
            if ev == "passthru.leg.eval":
                try:
                    return json.loads(data.get("out", "{}"))
                except ValueError:
                    return {"raw": data.get("out")}
        await asyncio.sleep(0.1)
    return {}


def words(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", s.lower()))


class Host:
    def __init__(self, log):
        self.log, self.proc, self.waiters = log, None, []

    async def start(self, cdp: str):
        self.proc = await asyncio.create_subprocess_exec("node", str(HERE / "testroom" / "meet_host.mjs"), cdp,
                                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.q = asyncio.Queue()
        asyncio.ensure_future(self._read())

    async def _read(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                await self.q.put({"ev": "exit"}); return
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            self.log("host." + ev.get("ev", "?"), {k: v for k, v in ev.items() if k not in ("ev", "t")})
            await self.q.put(ev)

    async def cmd(self, cmd: str, want: str, timeout: float, **data) -> dict:
        self.proc.stdin.write((json.dumps({"cmd": cmd, **data}) + "\n").encode()); await self.proc.stdin.drain()
        end = time.monotonic() + timeout
        while True:
            ev = await asyncio.wait_for(self.q.get(), max(0.1, end - time.monotonic()))
            if ev["ev"] in (want, "cmd.failed", "exit"):
                return ev


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"e2e-meet-{time.strftime('%Y%m%d-%H%M%S')}")
    ap.add_argument("--phrase", default="Hello from z zero. The orange kite lands at seven.")
    ap.add_argument("--host-phrase", default="blue heron flies at noon", help="words the host's fake mic says")
    ap.add_argument("--cdp", default=os.environ.get("Z0_MEET_HOST_CDP", "http://127.0.0.1:19377"))
    ap.add_argument("--identity", choices=["host", "guest", "env"], default="host",
                    help="z0's Google identity: host = the host browser's own account (joins with 'Join here too'), "
                         "guest = anonymous 'Ask to join' (Meet refuses automated guests as of Nov 2026), "
                         "env = Z0LIVE_PASSTHRU_STATE as already set")
    args = ap.parse_args()
    state: Path | None = None
    os.environ["Z0LIVE_PASSTHRU_DEBUG"] = "1"   # lets z0_rtp() read the meeting page's WebRTC stats
    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    logf = (out / "events.jsonl").open("w")

    def log(ev, data, quiet=False):
        line = redact(json.dumps({"t": round(time.time(), 2), "ev": ev, **data}))
        logf.write(line + "\n"); logf.flush()
        if not quiet:
            print(line[:240], flush=True)

    res: dict = {"ok": False}
    call = FakeCall(log)
    p = passthru.Passthru(call)
    p.floor.policy = "open"
    host = Host(log)
    voice = tts_wav(args.phrase, out / "z0_line.wav")
    try:
        await host.start(args.cdp)
        ev = await host.cmd("create", "created", 120)
        link = ev.get("link", "")
        assert link, f"host could not create a meeting: {ev}"
        res["created_at"] = ev["t"]
        res["identity"] = args.identity
        if args.identity == "host":
            state = Path(tempfile.mkdtemp(prefix="z0meet-")) / "state.json"
            ev = await host.cmd("state", "state", 30, path=str(state))
            os.environ["Z0LIVE_PASSTHRU_STATE"] = str(state)
        elif args.identity == "guest":
            os.environ.pop("Z0LIVE_PASSTHRU_STATE", None)
        await p.start(link)
        ev = await host.cmd("admit", "admitted", 120, secs=100, name="z0")
        res["admitted_at"], res["admit_clicks"], res["z0_in_call"] = ev["t"], ev.get("clicks"), ev.get("present")
        await p.send("shot", path=str(out / "z0_view.png"))
        assert ev.get("present"), "z0 never appeared in the host's call"
        # 1) room -> z0: the host's mic loops a phrase; count what reaches z0's ears.
        for _ in range(100):
            if p.ws is not None:
                break
            await asyncio.sleep(0.1)
        f0, t0 = p.room.received, time.monotonic()
        heard = []
        while time.monotonic() - t0 < 12:
            a = p.room.take(960)
            if a is not None:
                heard.append(a)
            await asyncio.sleep(0.01)
        res["room_frames_12s"] = p.room.received - f0
        ears = np.concatenate(heard) if heard else np.zeros(1, np.int16)
        res["z0_ears_peak"] = int(np.abs(ears).max())
        with wave.open(str(out / "z0_ears.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(ears.tobytes())
        await host.cmd("shot", "shot", 20, path=str(out / "host_in_call.png"))
        res["host_rtp_before"] = (await host.cmd("rtp", "rtp", 20)).get("audio")
        res["z0_rtp_before"] = await z0_rtp(p, call)
        # 2) z0 -> room: z0 speaks while the host records what it hears.
        secs = len(voice) / 48000 + 3
        rec = asyncio.ensure_future(host.cmd("record", "recorded", secs + 30, secs=secs, path=str(out / "host_heard.webm")))
        await asyncio.sleep(1.0)
        pts, start = 0, time.monotonic()
        res["z0_spoke_at"] = time.time()
        while pts < len(voice):
            chunk = voice[pts:pts + 960]
            p.voice(frame(np.pad(chunk, (0, 960 - len(chunk))), pts)); pts += 960
            await asyncio.sleep(max(0, start + pts / 48000 - time.monotonic()))
        ev = await rec
        res["host_rtp_after"] = (await host.cmd("rtp", "rtp", 20)).get("audio")
        res["z0_rtp_after"] = await z0_rtp(p, call)
        res["host_record"] = {k: ev.get(k) for k in ("bytes", "tracks", "peak_rms")}
        res["voice_sent"] = p.counts["voice_sent"]
        # The WebAudio mix and each remote track recorded on its own; keep the transcript that hears the most.
        best = ("", "")
        for src in [str(out / "host_heard.webm")] + [p_ for p_, n in ev.get("per", []) if n > 2000]:
            wav = Path(src).with_suffix(".wav")
            if subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", str(wav)]).returncode:
                continue
            text = transcribe(wav)
            if len(words(args.phrase) & words(text)) >= len(words(args.phrase) & words(best[1])):
                best = (wav.name, text)
        res["host_transcript_from"], res["host_transcript"] = best
        res["z0_ears_transcript"] = transcribe(out / "z0_ears.wav")
        await p.send("shot", path=str(out / "z0_view_end.png"))
        await asyncio.sleep(1.5)
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[:300]
        await p.send("shot", path=str(out / "z0_view_fail.png"))
        await asyncio.sleep(1.5)
    finally:
        try:
            await p.stop("test_done")
        except Exception as e:  # noqa: BLE001
            log("stop.error", {"detail": str(e)[:200]})
        if host.proc and host.proc.returncode is None:
            try:
                await host.cmd("leave", "left", 20)
            except Exception:  # noqa: BLE001
                host.proc.kill()
        res["room_frames_total"], res["room_dropped"] = p.room.received, p.room.dropped
        if state is not None:  # the cookie file never outlives the run
            state.unlink(missing_ok=True); state.parent.rmdir()
    stt = bool(os.environ.get("Z0_MEET_STT_PY"))
    phrase_hit = words(args.phrase) & words(res.get("host_transcript", ""))
    res["phrase_words_heard"] = sorted(phrase_hit)
    res["host_phrase_words_heard"] = sorted(words(args.host_phrase) & words(res.get("z0_ears_transcript", "")))
    res["ok"] = bool(res.get("z0_in_call") and res.get("room_frames_12s", 0) > 300 and res.get("z0_ears_peak", 0) > 900
                     and (res.get("host_record") or {}).get("peak_rms", 0) > 0.01
                     and (not stt or len(phrase_hit) >= 3))
    (out / "result.json").write_text(json.dumps(res, indent=2))
    print("RESULT", json.dumps(res))
    print("PASS" if res["ok"] else "FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

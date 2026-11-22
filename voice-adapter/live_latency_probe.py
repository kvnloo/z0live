"""First-audio latency on the real Live call path: offline Piper speech -> adapter (mobile_live.Call,
host delegation, real gpt-live-1-codex over the Codex subscription) -> real Hermes run(s) -> spoken answer.
No microphone; does not touch the running service. Usage:
  .venv/bin/python live_latency_probe.py WAV PROFILE ANSWER_REGEX OUT.json [TIMEOUT_S]
reuse-checked: live_speech_smoke.py -- it answers with a scripted client fixture, so it cannot time host delegations.
"""
import asyncio, json, re, sys, time
from array import array
from fractions import Fraction
from pathlib import Path

import av
from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack
import importlib.util, os
# PROBE_ADAPTER=<path> times another adapter revision (e.g. a .bak) on the same path.
import importlib.machinery
_path = os.environ.get('PROBE_ADAPTER', 'mobile_live.py')
_spec = importlib.util.spec_from_file_location('mobile_live', _path,
                                               loader=importlib.machinery.SourceFileLoader('mobile_live', _path))
live = importlib.util.module_from_spec(_spec)
sys.modules['mobile_live'] = live
_spec.loader.exec_module(live)

WAV, PROFILE, RX, OUT = sys.argv[1], sys.argv[2], re.compile(sys.argv[3], re.I), sys.argv[4]
T = {}


def mark(k):
    T.setdefault(k, time.monotonic())


class Speech(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        rs = av.AudioResampler(format='s16', layout='mono', rate=48000)
        parts = []
        with av.open(WAV) as c:
            for f in c.decode(audio=0):
                parts += [bytes(o.planes[0])[:o.samples * 2] for o in rs.resample(f)]
        self.pcm, self.i, self.t0 = bytes(48000 * 2) + b''.join(parts), 0, None

    async def recv(self):
        if self.t0 is None:
            self.t0 = time.monotonic()
        await asyncio.sleep(max(0, self.t0 + self.i / 48000 - time.monotonic()))
        if self.i * 2 >= len(self.pcm):
            mark('speech_end')
        pcm = self.pcm[self.i * 2:(self.i + 960) * 2]
        fr = av.AudioFrame(format='s16', layout='mono', samples=960)
        fr.planes[0].update(pcm + bytes(1920 - len(pcm)))
        fr.sample_rate, fr.pts, fr.time_base = 48000, self.i, Fraction(1, 48000)
        self.i += 960
        return fr


# Instrument without changing the adapter: when speakable delegation text reaches the provider.
spoken = []
_send = live.Call.send


def send(self, event, to_provider=False, **kwargs):
    if to_provider and event.get('type') == 'delegation.context.append' and event.get('channel') == 'speakable':
        txt = ''.join(c.get('text', '') for c in event.get('content', []))
        spoken.append((round(time.monotonic() - T.get('speech_end', time.monotonic()), 2), txt[:400]))
        mark('first_speakable_injected')
    return _send(self, event, to_provider, **kwargs)


live.Call.send = send
_hd = live.Call.host_delegate


async def host_delegate(self, did, request):
    mark('delegation')
    T['request'] = request
    await _hd(self, did, request)
    mark('driver_done')


live.Call.host_delegate = host_delegate


async def main():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc = pc.createDataChannel('oai-events')
    out = {'text': '', 'after_answer': ''}

    @dc.on('message')
    def msg(raw):
        e = json.loads(raw)
        if e.get('type') == 'session.output_transcript.delta':
            out['text'] += e.get('delta', '')
            if 'first_speakable_injected' in T:
                out['after_answer'] += e.get('delta', '')
                if RX.search(out['after_answer']):
                    mark('answer_transcript_match')

    @pc.on('track')
    def track(tr):
        async def drain():
            while True:
                f = await tr.recv()
                s = array('h', bytes(f.planes[0]))
                if s and max(map(abs, s)) > 500 and 'speech_end' in T:
                    mark('first_audio')
                    if 'first_speakable_injected' in T:
                        mark('first_answer_audio')
                    if 'answer_transcript_match' in T:
                        mark('correct_answer_audio_by')  # audio still playing once the answer text matched
        asyncio.ensure_future(drain())

    pc.addTrack(Speech())
    async with ClientSession() as http:
        m = live.Manager(http)
        call = live.Call(m, PROFILE)
        m.calls[call.id] = call
        try:
            await pc.setLocalDescription(await pc.createOffer())
            ans = await call.start(pc.localDescription.sdp, [])
            await pc.setRemoteDescription(RTCSessionDescription(sdp=ans['transport']['sdp'], type='answer'))
            try:
                async with asyncio.timeout(float(sys.argv[5]) if len(sys.argv) > 5 else 420):
                    while 'driver_done' not in T:  # the driver's final, success or failure
                        await asyncio.sleep(0.5)
                    await asyncio.sleep(12)  # let the voice speak it
            except TimeoutError:
                pass
        finally:
            e = T.get('speech_end')
            rel = {k: round(v - e, 2) for k, v in T.items() if isinstance(v, float) and e}
            r = {'profile': PROFILE, 'wav': WAV, 'request': T.get('request'),
                 'seconds_after_speech_end': rel, 'speakable_injections': spoken,
                 'assistant_transcript': out['text'][-1500:], 'answer_matched': 'answer_transcript_match' in T,
                 'adapter_counts': dict(call.counts)}
            Path(OUT).write_text(json.dumps(r, indent=2))
            print(json.dumps(r, indent=2))
            await call.close('probe_done')
            await pc.close()


asyncio.run(main())

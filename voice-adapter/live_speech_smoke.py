"""Real subscription acoustic/protocol test with offline synthetic speech, no mic.
The 'backend result' below is explicitly a scripted fixture, not a Hermes tool run.
"""
import asyncio
from array import array
from collections import Counter
from fractions import Fraction
import json
from pathlib import Path
import time
from tempfile import TemporaryDirectory

from aiohttp import ClientSession
import av
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack
import mobile_live as live

class PromptTrack(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
        parts = []
        with av.open('test-utterance.wav') as container:
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):parts.append(bytes(out.planes[0])[:out.samples*2])
            for out in resampler.resample(None):parts.append(bytes(out.planes[0])[:out.samples*2])
        self.pcm = bytes(48000*2) + b''.join(parts)
        self.index = 0
        self.started = None
    async def recv(self):
        if self.started is None:self.started=time.monotonic()
        await asyncio.sleep(max(0,self.started+self.index/48000-time.monotonic()))
        frame = av.AudioFrame(format='s16', layout='mono', samples=960)
        pcm = self.pcm[self.index*2:(self.index+960)*2]
        frame.planes[0].update(pcm+bytes(1920-len(pcm)))
        frame.sample_rate=48000;frame.pts=self.index;frame.time_base=Fraction(1,48000)
        self.index+=960
        return frame

async def main():
    client=RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc=client.createDataChannel('oai-events')
    counts, events = Counter(),Counter()
    tasks=[]
    final_received=asyncio.Event()
    reply_text=''
    @dc.on('message')
    def received(raw):
        nonlocal reply_text
        try:e=json.loads(raw)
        except ValueError:return
        kind=e.get('type','unknown');events[kind]+=1
        if kind=='session.delegation.created':
            did=e['delegation']['id']
            dc.send(json.dumps({'type':'session.thinking.append','delegation_id':did,'content':'Running the scripted connection fixture.'}))
            dc.send(json.dumps({'type':'session.commentary.append','delegation_id':did,'content':'The automated connection check passed.'}))
        if kind=='session.output_transcript.delta':
            reply_text+=e.get('delta','')
            if 'passed' in reply_text.lower():final_received.set()
        if kind=='error':print('Voice error:',str((e.get('error') or {}).get('message',''))[:200])
    @client.on('track')
    def track_received(track):
        async def drain():
            try:
                while True:
                    f=await track.recv();counts['audio_frames']+=1
                    samples=array('h',bytes(f.planes[0]))
                    if samples and max(map(abs,samples))>500:counts['non_silent_frames']+=1
            except Exception:pass
        tasks.append(asyncio.create_task(drain()))
    client.addTrack(PromptTrack())
    started=time.monotonic()
    async with ClientSession() as http:
        manager=live.Manager(http);call=live.Call(manager);manager.calls[call.id]=call
        try:
            await client.setLocalDescription(await client.createOffer())
            answer=await call.start(client.localDescription.sdp,[])
            await client.setRemoteDescription(RTCSessionDescription(sdp=answer['transport']['sdp'],type='answer'))
            async with asyncio.timeout(45):
                await final_received.wait()
                while counts['non_silent_frames']<10:await asyncio.sleep(.1)
                await asyncio.sleep(.5)
            assert events['session.input_transcript.delta']>0
            assert events['session.delegation.created']==1
            receipt={'real_subscription':True,'microphone_used':False,
                     'input':'offline synthetic connection-test utterance',
                     'backend':'scripted connection fixture, not a Hermes agent/tool turn',
                     'model':live.MODEL,'elapsed_seconds':round(time.monotonic()-started,2),
                     'mobile_peer':client.connectionState,'provider_peer':call.provider.connectionState,
                     'events':dict(events),'audio':dict(counts),'adapter_counts':dict(call.counts),
                     'expected_final_reply_detected':True}
            Path('live-speech-smoke-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
            print(json.dumps(receipt,indent=2))
        finally:
            print('Final state:',json.dumps({'events':dict(events),'audio':dict(counts),'adapter_counts':dict(call.counts)}))
            await manager.close();await client.close()
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

if __name__ == "__main__":
    with TemporaryDirectory(prefix="z0live-smoke-traces-") as traces:
        live.TRACE_DIR = Path(traces)
        asyncio.run(main())

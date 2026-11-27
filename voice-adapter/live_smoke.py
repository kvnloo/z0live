"""Real subscription smoke test: no microphone, no API key, close on exit."""
import asyncio
from collections import Counter
import json
from pathlib import Path
import time
from tempfile import TemporaryDirectory

from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, AudioStreamTrack
import mobile_live as live

async def main():
    client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    counts = Counter()
    events = Counter()
    tasks = []
    dc = client.createDataChannel('oai-events')
    @dc.on('message')
    def message(raw):
        try:
            e = json.loads(raw);events[e.get('type', 'unknown')] += 1
            if e.get('type') == 'error':
                print('Provider voice error:', str(e.get('error', {}).get('message', ''))[:300], flush=True)
        except Exception:
            pass
    @client.on('track')
    def track_received(track):
        async def drain():
            try:
                while True:
                    await track.recv();counts['received_audio_frames'] += 1
            except Exception:
                pass
        tasks.append(asyncio.create_task(drain()))
    client.addTrack(AudioStreamTrack())
    started = time.monotonic()
    async with ClientSession() as http:
        manager = live.Manager(http)
        call = live.Call(manager)
        manager.calls[call.id] = call
        try:
            await client.setLocalDescription(await client.createOffer())
            result = await call.start(client.localDescription.sdp, [])
            await client.setRemoteDescription(RTCSessionDescription(sdp=result['transport']['sdp'], type='answer'))
            async with asyncio.timeout(30):
                while dc.readyState != 'open' or call.provider_dc.readyState != 'open':
                    await asyncio.sleep(.1)
                dc.send(json.dumps({'type': 'session.commentary.append', 'delegation_id': None,
                                    'content': 'For this connection test, say only: Subscription voice is connected.'}))
                while not events['session.output_transcript.delta']:
                    await asyncio.sleep(.1)
                await asyncio.sleep(1)
            receipt = {'real_subscription': True, 'model': live.MODEL,
                       'elapsed_seconds': round(time.monotonic()-started, 2),
                       'mobile_peer': client.connectionState, 'provider_peer': call.provider.connectionState,
                       'audio': dict(counts), 'mobile_events': dict(events),
                       'adapter_counts': dict(call.counts)}
            Path('live-smoke-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
            print(json.dumps(receipt, indent=2))
        finally:
            print('Final connection evidence:', json.dumps({'client': client.connectionState,
                'provider': call.provider.connectionState, 'events': dict(events),
                'counts': dict(call.counts), 'audio': dict(counts)}), flush=True)
            await manager.close()
            await client.close()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    with TemporaryDirectory(prefix="z0live-smoke-traces-") as traces:
        live.TRACE_DIR = Path(traces)
        asyncio.run(main())

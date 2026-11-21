"""Review-gate repro for fix/fast-delegation (e692bbf). Run from voice-adapter/ of the head under test
(see repro_guest_floor.py). Uses the branch's own real-WebRTC renewal fixture; no network beyond loopback.

replace_subscription waits for in-flight hand-offs only until deadline - 2 s, then clears protocol.seen. A Hermes
answer that lands after the swap is filtered by Protocol.from_mobile ("did not in self.seen") and never reaches
the caller, with no trace event. Delegations run p50 39.7 s, p90 166 s (REVIEW-S-TIER.md section 4), and the
renewal budget is about 43 s, so a long hand-off at renewal time is the common case, not an edge.
"""
import asyncio
import json
from unittest.mock import patch

from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

import mobile_live as live
from test_mobile_live import IntegrationTests


class RenewalDropsAnswer(IntegrationTests):
    async def test_hermes_answer_in_flight_across_renewal_reaches_the_caller(self):
        await self.hand_off(renew=True)

    async def test_control_without_renewal_answer_reaches_the_caller(self):
        await self.hand_off(renew=False)

    async def hand_off(self, renew):
        phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(phone)
        phone.addTrack(AudioStreamTrack())
        dc = phone.createDataChannel('oai-events')
        await phone.setLocalDescription(await phone.createOffer())
        manager = self.app['manager']
        # No legacy lanes (nothing leaves loopback) and no guest from the owner's ~/.config/z0live/voices.json.
        with patch.object(live, 'TIERS_ON', True), patch.object(live.guest_voice, 'configured', return_value=None):
            call = live.Call(manager)
            manager.calls[call.id] = call
            release = asyncio.Event()

            async def hermes(did, request):  # Hermes still working when the renewal budget runs out
                await release.wait()
                return 'The deploy finished cleanly.'

            async def no_lanes(did, request):
                return None

            call.ask_hermes, call.tier_lanes = hermes, no_lanes
            result = await call.start(phone.localDescription.sdp, [])
            await phone.setRemoteDescription(RTCSessionDescription(sdp=result['transport']['sdp'], type='answer'))
            async with asyncio.timeout(15):
                while dc.readyState != 'open' or not self.provider_channels:
                    await asyncio.sleep(.01)
                self.provider_channels[0].send(json.dumps({'type': 'delegation.created', 'item': {
                    'id': 'd1', 'target': 'client', 'content': [{'type': 'input_text', 'text': 'Is the deploy done?'}]}}))
                while not call.pending_delegations:
                    await asyncio.sleep(.01)
                if renew:
                    await call.replace_subscription(live.time.monotonic() + 3)
                    self.assertTrue(call.pending_delegations, 'fixture: the hand-off must still be running at the swap')
                    self.assertEqual(len(self.provider_channels), 2, 'fixture: the swap must have happened')
                while not self.received.empty():
                    self.received.get_nowait()
                release.set()
                while call.pending_delegations:
                    await asyncio.sleep(.01)
            spoken = []
            try:
                async with asyncio.timeout(1.5):
                    while True:
                        spoken.append(json.dumps(await self.received.get()))
            except TimeoutError:
                pass
            self.assertTrue(any('The deploy finished cleanly.' in s for s in spoken),
                            'the Hermes answer was dropped after the renewal swap')


def load_tests(loader, tests, pattern):  # run only the repro, not the inherited suite
    names = ('test_hermes_answer_in_flight_across_renewal_reaches_the_caller',
             'test_control_without_renewal_answer_reaches_the_caller')
    return loader.suiteClass(RenewalDropsAnswer(name) for name in names)

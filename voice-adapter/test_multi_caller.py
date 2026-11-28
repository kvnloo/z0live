"""Multi-caller mixing, turn attribution, invites and the people-to-people signalling mailbox."""
from collections import Counter
import json
import os
import unittest
from unittest import mock

import av
import numpy as np

import multi_caller as mc


def pcm(value, n=1920):
    return np.full(n, value, dtype=np.int16)


def frame(value, samples=960):
    f = av.AudioFrame(format="s16", layout="stereo", samples=samples)
    f.sample_rate = 48000
    f.planes[0].update(pcm(value, samples * 2).tobytes())
    return f


def decoded_frame(value):
    """A frame as aiortc hands it over: through a real Opus encode/decode, so its plane is padded."""
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate, enc.layout, enc.format = 48000, "stereo", "s16"
    enc.open()
    dec = av.CodecContext.create("libopus", "r")
    dec.sample_rate, dec.layout, dec.format = 48000, "stereo", "s16"
    src = av.AudioFrame.from_ndarray(pcm(value).reshape(1, -1), format="s16", layout="stereo")
    src.sample_rate = 48000
    for _ in range(10):
        for packet in enc.encode(src):
            for out in dec.decode(packet):
                last = out
    last.planes[0].update(pcm(value).tobytes() + bytes(last.planes[0].buffer_size - 3840))  # exact samples
    return last


class FakeCall:
    id = "hermes-subscription-test"
    profile = "guest"
    closed = False

    def __init__(self):
        self.counts = Counter()
        self.events = []

    def trace(self, ev, **data):
        self.events.append((ev, data))


class MixTest(unittest.TestCase):
    def test_sums_callers(self):
        out, skipped = mc.mix(pcm(1000), [pcm(2000), pcm(-500)])
        self.assertEqual(skipped, 0)
        self.assertTrue((out == 2500).all())
        self.assertEqual(out.dtype, np.int16)

    def test_saturates_instead_of_wrapping(self):
        out, _ = mc.mix(pcm(30000), [pcm(10000)])
        self.assertTrue((out == 32767).all())
        out, _ = mc.mix(pcm(-30000), [pcm(-10000)])
        self.assertTrue((out == -32768).all())

    def test_alone_is_unchanged(self):
        host = pcm(123)
        out, skipped = mc.mix(host, [])
        self.assertIs(out, host)
        self.assertEqual(skipped, 0)

    def test_shape_mismatch_is_skipped_not_resampled(self):
        out, skipped = mc.mix(pcm(100), [pcm(100, n=960), pcm(50)])
        self.assertEqual(skipped, 1)
        self.assertTrue((out == 150).all())


class AttributionTest(unittest.TestCase):
    def test_dominant_speaker_and_overlap(self):
        a = mc.Attribution()
        for _ in range(3):
            a.frame({"host": 5000, "guest-1": 0})
        for _ in range(2):
            a.frame({"host": 5000, "guest-1": 4000})  # both at once
        for _ in range(6):
            a.frame({"host": 0, "guest-1": 4000})
        turn = a.close_turn()
        self.assertEqual(turn["speaker"], "guest-1")
        self.assertEqual(turn["speakers"], ["guest-1", "host"])
        self.assertEqual(turn["loud_frames"], {"host": 5, "guest-1": 8})
        self.assertEqual(turn["overlap_frames"], 2)
        self.assertEqual(a.close_turn()["speaker"], None)  # reset after each turn

    def test_tie_goes_to_who_spoke_first(self):
        a = mc.Attribution()
        a.frame({"host": 0, "guest-1": 900})
        a.frame({"host": 900, "guest-1": 0})
        self.assertEqual(a.close_turn()["speaker"], "guest-1")

    def test_quiet_frames_name_nobody(self):
        a = mc.Attribution()
        a.frame({"host": mc.LOUD - 1, "guest-1": 10})
        turn = a.close_turn()
        self.assertIsNone(turn["speaker"])
        self.assertEqual(turn["frames"], 1)


class InviteTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.invites = mc.Invites(clock=lambda: self.now)

    def test_single_use(self):
        secret = self.invites.issue("call-1")
        self.assertEqual(self.invites.redeem(secret), "call-1")
        self.assertIsNone(self.invites.redeem(secret))

    def test_expires(self):
        secret = self.invites.issue("call-1", seconds=10)
        self.now += 10
        self.assertIsNone(self.invites.redeem(secret))

    def test_wrong_or_malformed(self):
        self.invites.issue("call-1")
        for bad in (None, 5, "", "x" * 19, "x" * 200, "y" * 43):
            self.assertIsNone(self.invites.redeem(bad))

    def test_only_digest_is_stored_and_revoke(self):
        secret = self.invites.issue("call-1")
        self.assertNotIn(secret, repr(self.invites.live))
        self.invites.revoke_call("call-1")
        self.assertIsNone(self.invites.redeem(secret))


class MailboxTest(unittest.TestCase):
    def test_routes_to_recipient_only(self):
        box = mc.Mailbox()
        members = ["host", "guest-1", "guest-2"]
        box.post("guest-1", members, "host", "offer", "v=0 offer")
        box.post("host", members, "guest-1", "answer", "v=0 answer")
        self.assertEqual([m["kind"] for m in box.read("host")], ["offer"])
        self.assertEqual(box.read("guest-1")[0]["sdp"], "v=0 answer")
        self.assertEqual(box.read("guest-2"), [])
        self.assertEqual(box.read("host", after=1), [])

    def test_rejects_bad_messages(self):
        box = mc.Mailbox()
        members = ["host", "guest-1"]
        with self.assertRaises(ValueError):
            box.post("host", members, "stranger", "offer", "v=0")
        with self.assertRaises(ValueError):
            box.post("host", members, "host", "offer", "v=0")
        with self.assertRaises(ValueError):
            box.post("host", members, "guest-1", "candidate", "v=0")
        with self.assertRaises(ValueError):
            box.post("host", members, "guest-1", "offer", "not sdp")
        self.assertEqual(box.post("host", members, "guest-1", "bye")["seq"], 1)

    def test_bounded(self):
        box = mc.Mailbox(maxlen=3)
        for _ in range(5):
            box.post("host", ["host", "g"], "g", "bye")
        self.assertEqual([m["seq"] for m in box.read("g")], [3, 4, 5])


class RoomMixTest(unittest.TestCase):
    def setUp(self):
        self.call = FakeCall()
        self.room = mc.Room(self.call, invites=mc.Invites())
        self.guest = mc.Caller("guest-1", "bob", "a" * 32, "100.0.0.2")
        self.room.callers["guest-1"] = self.guest

    def test_guest_audio_reaches_the_provider_stream(self):
        self.guest.queue.append(pcm(3000))
        out = self.room.mix_frame(frame(1000))
        got = np.frombuffer(out.planes[0], dtype=np.int16, count=1920)
        self.assertTrue((got == 4000).all())
        self.assertEqual(self.call.counts["caller_mixed:guest-1"], 1)
        self.assertIn(("caller.audio.on", {"caller": "guest-1", "peak": 3000}), self.call.events)
        self.assertIn(("caller.audio.on", {"caller": "host", "peak": 1000}), self.call.events)

    def test_decoded_frame_with_padded_plane(self):
        # A WebRTC (Opus-decoded) frame's plane is padded far past its samples (23040 vs 3840 bytes); the first
        # live two-host run died here: plane.update(<3840 bytes>) raised and ended the host's uplink.
        decoded = decoded_frame(1000)
        self.assertGreater(decoded.planes[0].buffer_size, 960 * 2 * 2)
        self.guest.queue.append(pcm(3000))
        out = self.room.mix_frame(decoded)
        got = np.frombuffer(out.planes[0], dtype=np.int16, count=1920)
        self.assertTrue((got == 4000).all(), got[:8])

    def test_mix_failure_never_breaks_the_uplink(self):
        f = frame(1000)
        self.guest.queue.append(pcm(3000))
        with mock.patch.object(mc, "_write", side_effect=RuntimeError("boom")):
            out = self.room.mix_frame(f)
        self.assertIs(out, f)
        self.assertEqual(self.call.counts["caller_mix_error"], 1)

    def test_missing_guest_frame_passes_host_through(self):
        out = self.room.mix_frame(frame(1000))
        got = np.frombuffer(out.planes[0], dtype=np.int16, count=1920)
        self.assertTrue((got == 1000).all())

    def test_guest_queue_drops_oldest(self):
        for v in range(mc.QUEUE_FRAMES + 3):
            self.guest.queue.append(pcm(v))
        self.assertEqual(len(self.guest.queue), mc.QUEUE_FRAMES)
        self.assertEqual(int(self.guest.queue[0][0]), 3)

    def test_turn_names_the_speaker(self):
        for _ in range(4):
            self.guest.queue.append(pcm(5000))
            self.room.mix_frame(frame(0))
        self.room.end_turn("hi it's bob")
        ev, data = self.call.events[-1]
        self.assertEqual(ev, "caller.turn")
        self.assertEqual(data["speaker"], "guest-1")
        self.assertEqual(data["text"], "hi it's bob")
        self.room.end_turn("")  # a silent turn traces nothing
        self.assertEqual(self.call.events[-1][0], "caller.turn")
        self.assertEqual(len([e for e in self.call.events if e[0] == "caller.turn"]), 1)

    def test_member_tokens(self):
        self.assertEqual(self.room.member(self.room.callers["host"].member), "host")
        self.assertEqual(self.room.member(self.room.callers["guest-1"].member), "guest-1")
        self.assertIsNone(self.room.member("nope"))
        self.assertIsNone(self.room.member(None))


class FlagTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(mc.enabled())
        with mock.patch.dict(os.environ, {"Z0LIVE_MULTI_CALLER": "on"}):
            self.assertTrue(mc.enabled())


class GuestProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_calls_cannot_open_rooms(self):
        import mobile_live as live

        with mock.patch.dict(os.environ, {"Z0LIVE_MULTI_CALLER": "on", "HERMES_MOBILE_VOICE_TRACE": "0"}), \
             mock.patch.object(live, "voice_engine", return_value="gpt-live"):
            for profile in ("", "tek", "pc", "guest"):
                call = live.Call(live.Manager(None), profile=profile)
                self.addAsyncCleanup(call.close, "test")
                self.assertEqual(call.multi is not None, profile == "guest")

    async def test_guest_delegation_cannot_enter_the_owner_pipeline(self):
        import mobile_live as live

        with mock.patch.dict(os.environ, {"Z0LIVE_MULTI_CALLER": "on", "HERMES_MOBILE_VOICE_TRACE": "0"}), \
             mock.patch.object(live, "voice_engine", return_value="gpt-live"):
            call = live.Call(live.Manager(None), profile="guest")
        self.addAsyncCleanup(call.close, "test")
        self.assertIsNone(call.relay)
        self.assertIsNone(call.passthru)
        self.assertIsNone(call.guest)
        self.assertTrue(call.protocol.host_delegation)
        call.lane_command = mock.Mock()
        call.bot_ask = mock.Mock()
        call.ask_hermes = mock.AsyncMock()
        call.speak_for = mock.Mock()
        query = "ask the owner profile for its chat history"
        with mock.patch.object(live, "ask_guest_host", side_effect=["Guest answer", OSError()]) as guest:
            await call.host_delegate("first", query)
            await call.host_delegate("second", query)
        self.assertEqual(guest.call_args_list, [mock.call(None, "guest", call.id, query)] * 2)
        call.lane_command.assert_not_called()
        call.bot_ask.assert_not_called()
        call.ask_hermes.assert_not_called()
        call.speak_for.assert_called_with("second", "The guest profile is unavailable.")

        call.send = mock.Mock()
        call.message(json.dumps({"type": "session.commentary.append", "content": "private context"}), False)
        call.send.assert_not_called()

    async def test_bad_sdp_releases_guest_slot(self):
        room = mc.Room(FakeCall())
        room.output_ready.set()
        peer = mock.Mock()
        peer.setRemoteDescription = mock.AsyncMock(side_effect=ValueError("bad media"))
        peer.close = mock.AsyncMock()
        with mock.patch("aiortc.RTCPeerConnection", return_value=peer):
            with self.assertRaises(ValueError):
                await room.join("v=0", "example", "127.0.0.1")
        self.assertEqual(list(room.callers), [mc.HOST])
        self.assertIsNone(room.member("é" * 32))
        peer.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

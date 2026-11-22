import unittest
import av
import numpy as np

import passthru


def frame(value=0, layout="stereo", fmt="s16", samples=960):
    ch = 2 if layout == "stereo" else 1
    shape = (ch, samples) if fmt.endswith("p") else (1, samples * ch)
    f = av.AudioFrame.from_ndarray(np.full(shape, value, dtype=np.int16), format=fmt, layout=layout)
    f.sample_rate, f.pts = 48000, 4800
    return f


class Commands(unittest.TestCase):
    def test_join_needs_name_or_merge_outside_passthru(self):
        self.assertEqual(passthru.command("z0, join my meeting", active=False), ("join", ""))
        self.assertEqual(passthru.command("merge the calls", active=False), ("join", ""))
        self.assertEqual(passthru.command("turn on passthru mode", active=False), ("join", ""))
        self.assertIsNone(passthru.command("I need to join my meeting at three", active=False))
        self.assertIsNone(passthru.command("share my screen", active=False))

    def test_join_carries_a_link(self):
        self.assertEqual(passthru.command("hey z0 join this call https://meet.google.com/abc-defg-hij", active=False),
                         ("join", "https://meet.google.com/abc-defg-hij"))

    def test_in_passthru_commands(self):
        self.assertEqual(passthru.command("share my phone", active=True), ("share", "phone"))
        self.assertEqual(passthru.command("z0 share the canvas please", active=True), ("share", "canvas"))
        self.assertEqual(passthru.command("ok stop sharing", active=True), ("unshare", ""))
        self.assertEqual(passthru.command("z0 leave the meeting", active=True), ("leave", ""))
        self.assertEqual(passthru.command("you can talk freely", active=True), ("floor", "open"))
        self.assertEqual(passthru.command("just listen for now", active=True), ("floor", "listen"))
        self.assertIsNone(passthru.command("the canvas looks great", active=True))

    def test_addressed(self):
        for t in ("z0, what do you think?", "Hey zero can you check", "okay Hermes, summarize"):
            self.assertTrue(passthru.addressed(t), t)
        for t in ("we start from zero today", "the hermes bag", ""):
            self.assertFalse(passthru.addressed(t), t)


class Floor(unittest.TestCase):
    def test_addressed_policy(self):
        f = passthru.Floor(policy="addressed")
        f.reply_started(10); self.assertFalse(f.may_speak(10))
        f.reply_done(11)
        f.user_said("z0, what's the plan?", 12); f.reply_started(13)
        self.assertTrue(f.may_speak(13))
        f.reply_done(15); self.assertTrue(f.may_speak(15.5)); self.assertFalse(f.may_speak(17))
        f.reply_started(40); self.assertFalse(f.may_speak(40))   # the release does not last forever

    def test_open_and_listen(self):
        self.assertTrue(passthru.Floor(policy="open").may_speak(0))
        f = passthru.Floor(policy="listen"); f.user_said("z0, talk", 0); f.reply_started(0)
        self.assertFalse(f.may_speak(0))


class Audio(unittest.TestCase):
    def test_room_bounded_and_chunked(self):
        r = passthru.RoomAudio()
        r.push(np.full(960 * 20, 1000, dtype=np.int16).tobytes())
        self.assertEqual(len(r.chunks), passthru.MAX_ROOM_FRAMES)
        self.assertEqual(r.dropped, 20 - passthru.MAX_ROOM_FRAMES)
        got = r.take(480); self.assertEqual(got.shape, (480,))
        self.assertIsNone(passthru.RoomAudio().take(960))

    def test_mix_packed_stereo_and_planar(self):
        room = np.full(960, 100, dtype=np.int16)
        for fmt in ("s16", "s16p"):
            out = passthru.mix_into(frame(50, fmt=fmt), room)
            self.assertTrue((out.to_ndarray() == 150).all(), fmt)
            self.assertEqual((out.pts, out.sample_rate, out.samples), (4800, 48000, 960))

    def test_mix_clips(self):
        out = passthru.mix_into(frame(32000), np.full(960, 32000, dtype=np.int16))
        self.assertEqual(int(out.to_ndarray().max()), 32767)

    def test_to_mono(self):
        m = passthru.to_mono_s16(frame(300))
        self.assertEqual((m.shape, int(m[0])), ((960,), 300))
        self.assertEqual(passthru.to_mono_s16(frame(7, layout="mono")).shape, (960,))


class Links(unittest.TestCase):
    def test_calendar_first_then_notifications(self):
        now = 1_800_000_000.0
        cal = ("Row: 0 title=standup, begin=%d, end=%d, eventLocation=https://meet.google.com/aaa-bbbb-ccc, description=NULL\n"
               "Row: 1 title=later, begin=%d, end=%d, eventLocation=https://zoom.us/j/999, description=NULL"
               % ((now - 60) * 1000, (now + 1800) * 1000, (now + 7200) * 1000, (now + 9000) * 1000))
        out = {"content": cal, "dumpsys notification": "text=join https://zoom.us/j/12345?pwd=x now",
               "dumpsys activity": ""}
        def adb(*args):
            key = args[0] if args[0] == "content" else " ".join(args[:2])
            return out.get(key, "")
        links = passthru.phone_links(adb, now)
        self.assertEqual(links[0], ("https://meet.google.com/aaa-bbbb-ccc", "calendar"))
        self.assertIn(("https://zoom.us/j/12345?pwd=x", "notification"), links)
        self.assertNotIn("https://zoom.us/j/999", [u for u, _ in links])


if __name__ == "__main__":
    unittest.main()

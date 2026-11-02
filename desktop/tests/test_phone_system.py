"""System phone lane: grammar, closed catalog, adb readback, watcher and Delegator routing.

The phone is a fake adb shell that keeps real state (volumes, zen_mode, Bluetooth,
brightness, a media session) and answers the exact commands the lane sends, so every
`landed` claim is checked against state the fake actually changed.
"""

import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import phone_system as ps  # noqa: E402
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop.delegation import Delegator  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bidi import FakeCall, FakeHermes, NoRoute  # noqa: E402


class FakeShell:
    """adb shell for a phone with real mutable state."""

    def __init__(self, stuck=()):
        self.vol = {3: 6, 2: 10, 5: 9, 4: 12, 0: 4}
        self.top = {3: 15, 2: 15, 5: 15, 4: 15, 0: 5}
        self.zen, self.bt, self.bright, self.mode = "0", "0", 171, "1"
        self.player = None  # {"state": "PLAYING"|"PAUSED"}
        self.stuck = set(stuck)  # settings that ignore writes, to test landed=false
        self.commands = []

    def sh(self, command):
        self.commands.append(command)
        m = re.fullmatch(r"cmd media_session volume --stream (\d) --(get|set)(?: (\d+))?", command)
        if m:
            s = int(m.group(1))
            if m.group(2) == "set" and "volume" not in self.stuck:
                self.vol[s] = int(m.group(3))
            return f"[V] Connecting to AudioService\n[V] volume is {self.vol[s]} in range [0..{self.top[s]}]\n"
        if command.startswith("cmd notification set_dnd "):
            if "dnd" not in self.stuck:
                self.zen = {"priority": "1", "off": "0", "alarms": "3"}[command.split()[-1]]
            return ""
        if command == "settings get global zen_mode":
            return self.zen + "\n"
        if command.startswith("cmd bluetooth_manager "):
            if "bt" not in self.stuck:
                self.bt = "1" if command.endswith("enable") else "0"
            return ""
        if command == "settings get global bluetooth_on":
            return self.bt + "\n"
        if command == "settings put system screen_brightness_mode 0":
            self.mode = "0"
            return ""
        if command == "settings put system screen_brightness_mode 1":
            self.mode = "1"
            return ""
        m = re.fullmatch(r"settings put system screen_brightness (\d+)", command)
        if m:
            self.bright = int(m.group(1))
            return ""
        if command == "settings get system screen_brightness":
            return f"{self.bright}\n"
        if command == "settings get system screen_brightness_mode":
            return self.mode + "\n"
        if command == "dumpsys media_session":
            block = ""
            if self.player:
                block = ("  Spotify com.spotify.music/spotify-media-session/2 (userId=0)\n"
                         "    package=com.spotify.music\n    active=true\n"
                         f"    state=PlaybackState {{state={self.player['state']}(3), position=1}}\n")
            return ("=== DETAILED SESSION INFO ===\n9 sessions listeners.\n"
                    "  HeadsetMediaButton com.android.server.telecom/HeadsetMediaButton/1 (userId=0)\n"
                    "    package=com.android.server.telecom\n    active=false\n    state=null\n"
                    + block + "User Records:\n")
        m = re.fullmatch(r"cmd media_session dispatch (\w+)", command)
        if m:
            if self.player and m.group(1) in {"play", "pause"} and "media" not in self.stuck:
                self.player["state"] = "PLAYING" if m.group(1) == "play" else "PAUSED"
            return ""
        raise AssertionError("unexpected adb command: " + command)


def system(shell):
    return ps.System(shell, sleep=lambda s: None)


class GrammarTests(unittest.TestCase):
    def test_owner_phrasings_map_to_one_action(self):
        cases = {
            "set the volume to 30 percent": ("volume_set", {"stream": "media", "percent": 30}),
            "volume to fifty": ("volume_set", {"stream": "media", "percent": 50}),
            "ringer volume to 0": ("volume_set", {"stream": "ring", "percent": 0}),
            "make it louder a lot": ("volume_step", {"stream": "media", "direction": "up", "big": True}),
            "turn the music down": ("volume_step", {"stream": "media", "direction": "down", "big": False}),
            "max volume": ("volume_set", {"stream": "media", "percent": 100}),
            "mute": ("mute", {}),
            "unmute the music": ("unmute", {}),
            "turn on do not disturb": ("dnd", {"mode": "on"}),
            "turn off dnd": ("dnd", {"mode": "off"}),
            "Can you turn Bluetooth on please?": ("bluetooth", {"enabled": True}),
            "brightness 70%": ("brightness_set", {"percent": 70}),
            "make the screen dimmer": ("brightness_step", {"direction": "down"}),
            "pause the music": ("media", {"action": "pause"}),
            "skip this song": ("media", {"action": "next"}),
            "go back a song": ("media", {"action": "previous"}),
            "what's the volume": ("status", {}),
        }
        for text, (action, args) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ps.parse(text), {"action": action, "args": args})

    def test_questions_relays_and_app_launches_are_not_system_actions(self):
        for text in ("why is the volume so low", "tell hermes to mute the build logs",
                     "open spotify", "did you turn bluetooth off", "what's the weather"):
            with self.subTest(text=text):
                self.assertIsNone(ps.parse(text))

    def test_model_output_outside_the_catalog_is_rejected(self):
        self.assertIsNone(ps.validate({"action": "wifi", "args": {"enabled": False}}))
        self.assertIsNone(ps.validate({"action": "factory_reset", "args": {}}))
        self.assertIsNone(ps.validate({"action": "volume_set", "args": {"stream": "media", "percent": 400}}))
        self.assertIsNone(ps.validate({"action": "volume_set", "args": {"stream": "system", "percent": 4}}))
        self.assertEqual(ps.validate({"action": "dnd", "args": {"mode": "on"}}),
                         {"action": "dnd", "args": {"mode": "on"}})


class SystemTests(unittest.TestCase):
    def test_volume_set_reads_the_level_back(self):
        shell = FakeShell()
        out = system(shell).run("volume_set", {"stream": "media", "percent": 40})
        self.assertEqual(shell.vol[3], 6)
        self.assertTrue(out["landed"])
        self.assertEqual(out["say"], "Media volume is at 40 percent.")

    def test_a_setting_that_did_not_change_is_reported_not_claimed(self):
        shell = FakeShell(stuck={"volume"})
        out = system(shell).run("volume_set", {"stream": "media", "percent": 100})
        self.assertFalse(out["landed"])
        self.assertIn("reads 40 percent", out["say"])

    def test_mute_then_unmute_restores_the_previous_level(self):
        shell = FakeShell()
        lane = system(shell)
        self.assertTrue(lane.run("mute", {})["landed"])
        self.assertEqual(shell.vol[3], 0)
        self.assertTrue(lane.run("unmute", {})["landed"])
        self.assertEqual(shell.vol[3], 6)

    def test_step_at_the_limit_says_so(self):
        shell = FakeShell()
        shell.vol[3] = 15
        out = system(shell).run("volume_step", {"stream": "media", "direction": "up"})
        self.assertEqual(out["say"], "Media volume is already at max.")

    def test_dnd_bluetooth_and_brightness_land(self):
        shell = FakeShell()
        lane = system(shell)
        self.assertEqual(lane.run("dnd", {"mode": "on"})["say"], "Do not disturb is on, priority only.")
        self.assertEqual(shell.zen, "1")
        self.assertTrue(lane.run("bluetooth", {"enabled": True})["landed"])
        out = lane.run("brightness_set", {"percent": 50})
        self.assertEqual((shell.bright, shell.mode), (128, "0"))
        self.assertTrue(out["landed"])

    def test_stuck_bluetooth_is_not_claimed(self):
        out = system(FakeShell(stuck={"bt"})).run("bluetooth", {"enabled": True})
        self.assertFalse(out["landed"])
        self.assertIn("hasn't changed", out["say"])

    def test_media_pause_reads_the_player_state(self):
        shell = FakeShell()
        shell.player = {"state": "PLAYING"}
        out = system(shell).run("media", {"action": "pause"})
        self.assertTrue(out["landed"])
        self.assertEqual(out["say"], "Paused.")
        shell.stuck.add("media")
        out = system(shell).run("media", {"action": "play"})
        self.assertFalse(out["landed"])

    def test_play_with_no_player_says_nothing_is_playing(self):
        out = system(FakeShell()).run("media", {"action": "play"})
        self.assertEqual(out["say"], "Nothing is set up to play right now.")

    def test_unreachable_phone_is_a_spoken_failure(self):
        class Down:
            def sh(self, command):
                raise ps.PhoneUnavailable("adb_unavailable")
        out = ps.System(Down()).run("dnd", {"mode": "on"})
        self.assertEqual((out["ok"], out["status"]), (False, "adb_unavailable"))


class AdbTransportTests(unittest.TestCase):
    def runner(self, devices, serials):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = devices if argv[1:] == ["devices"] else serials.get(argv[2], "") + "\n"
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        return run, calls

    def test_pinned_serial_prefers_the_tailnet_transport(self):
        devices = ("List of devices attached\nRFC123 device usb:1\nadb-R5C-x._adb-tls-connect._tcp device\n"
                   "100.1.2.3:39057 device\n")
        run, _ = self.runner(devices, {"RFC123": "RFC123", "adb-R5C-x._adb-tls-connect._tcp": "R5C",
                                       "100.1.2.3:39057": "R5C"})
        self.assertEqual(ps.Adb("R5C", run).transport(), "100.1.2.3:39057")

    def test_unpinned_requires_exactly_one_tailnet_phone(self):
        devices = "List of devices attached\n100.1.2.3:1 device\n100.1.2.4:1 device\n"
        run, _ = self.runner(devices, {})
        self.assertIsNone(ps.Adb("", run).transport())


class LaneTests(unittest.TestCase):
    def test_grammar_hit_never_calls_a_model(self):
        class Boom:
            def choose(self, text):
                raise AssertionError("model called")
        decision, result, trail = ps.Lane(system(FakeShell()), Boom()).handle("turn on do not disturb")
        self.assertEqual(decision["tier"], "grammar")
        self.assertTrue(result["landed"])

    def test_unparsed_system_words_go_to_the_fast_model(self):
        class Pick:
            calls = []

            def choose(self, text):
                self.calls.append(text)
                return {"action": "dnd", "args": {"mode": "on"}}, {"tier": "fast_model", "ms": 90}
        decision, result, _ = ps.Lane(system(FakeShell()), Pick()).handle(
            "I don't want any notifications for a while, silence everything please")
        self.assertEqual((decision["tier"], decision["action"]), ("fast_model", "dnd"))
        self.assertTrue(result["landed"])

    def test_non_system_words_skip_the_model(self):
        class Boom:
            def choose(self, text):
                raise AssertionError("model called")
        self.assertEqual(ps.Lane(system(FakeShell()), Boom()).handle("what did hermes say")[:2], (None, None))

    def test_chain_falls_through_a_refusing_endpoint(self):
        import io
        import json
        import urllib.error

        def opener(req, timeout):
            if "router" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b"{}"))
            return io.BytesIO(json.dumps({"model": "qwen", "choices": [{"message": {"content": '{"action": null}'}}]}).encode())
        chat = ps.chain_chat([{"url": "http://router/x", "model": "r"}, {"url": "http://groot/x", "model": "q"}],
                             opener=opener)
        self.assertEqual(chat("x")[2], "qwen")

    def test_watcher_records_a_verdict_and_never_acts(self):
        shell = FakeShell()
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            path = Path(tmp) / "watch.jsonl"
            watcher = ps.Watcher(path, chat=lambda p: ('{"verdict": "wrong", "reason": "asked to dim"}', 40.0, "m"),
                                 enabled=True)
            decision, result, _ = ps.Lane(system(shell), watcher=watcher).handle("turn off dnd")
            before = list(shell.commands)
            record = watcher.review("the screen is blinding me", decision, result, sync=True)
            self.assertEqual(record["verdict"], "wrong")
            self.assertEqual(shell.commands, before)  # the watcher sent nothing to the phone
            self.assertTrue(path.read_text().strip())


class DelegatorRoutingTests(unittest.TestCase):
    def setUp(self):
        for p in (patch("z0live_desktop.voice_runtime.load_plan", return_value=None),
                  patch("z0live_desktop.phone_system.warm"),
                  patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "bidi"})):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("unit"))
        with patch("z0live_desktop.core.mint", side_effect=AssertionError("bidi must not mint")):
            self.sid = self.store.new_session(
                self.device, {"client": "lawnchair", "tools": [], "transports": ["bidi", "realtime"]})["session_id"]

    def run_text(self, text, delegator):
        call = FakeCall(self.sid)
        delegator.delegate(call, "item_" + str(abs(hash(text))), text)
        end = time.time() + 5
        while time.time() < end and not any(c is None for _, _, c in call.appended):
            time.sleep(0.02)
        time.sleep(0.05)
        return [t for _, t, c in call.appended if c is None]

    def test_system_words_are_answered_by_the_phone_not_hermes(self):
        shell, voice, phone = FakeShell(), FakeHermes(), FakeHermes()
        d = Delegator(self.store, hermes=voice, router=NoRoute(), system=ps.Lane(system(shell), watcher=None),
                      phone_hermes=phone)
        finals = self.run_text("set the volume to 80 percent", d)
        self.assertIn("Media volume is at 80 percent.", finals[-1])
        self.assertEqual(shell.vol[3], 12)
        self.assertEqual((voice.started, phone.started), ([], []))
        self.assertEqual(d.trace(self.sid)[0]["route"], "system:volume_set")

    def test_multi_step_phone_work_goes_to_the_phone_session(self):
        voice, phone = FakeHermes(), FakeHermes(output="Hotspot is on.")
        d = Delegator(self.store, hermes=voice, router=NoRoute(),
                      system=ps.Lane(system(FakeShell()), chooser=type("N", (), {"choose": lambda s, t: (None, {})})(),
                                     watcher=None),
                      phone_hermes=phone)
        with patch.dict(os.environ, {"Z0LIVE_PHONE_SERIALNO": "R5CTEST"}):
            finals = self.run_text("open the hotspot settings and turn it on", d)
        self.assertTrue(phone.started and not voice.started)
        self.assertIn("PHONE LANE", phone.started[0][1])
        self.assertIn("R5CTEST", phone.started[0][1])
        self.assertIn("Hotspot is on.", finals[-1])

    def test_ordinary_work_stays_in_the_voice_session(self):
        voice, phone = FakeHermes(), FakeHermes()
        d = Delegator(self.store, hermes=voice, router=NoRoute(), system=ps.Lane(system(FakeShell()), watcher=None),
                      phone_hermes=phone)
        self.run_text("what's left on the dash release", d)
        self.assertTrue(voice.started and not phone.started)

    def test_lane_is_off_unless_the_deployment_opts_in(self):
        with patch.dict(os.environ, {"Z0LIVE_PHONE_SYSTEM": ""}):
            d = Delegator(self.store, hermes=FakeHermes(), router=NoRoute())
        self.assertIsNone(d.system)
        self.assertIsNone(d.phone_hermes)


if __name__ == "__main__":
    unittest.main()

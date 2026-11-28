"""Exercise the root Hermes plugin with temporary files and no provider calls."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("z0live_plugin", ROOT / "__init__.py")
assert _spec is not None and _spec.loader is not None
z = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(z)


class PluginTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        paths = {"lanes_file": self.tmp / "lanes.json", "tiers_file": self.tmp / "tiers.json",
                 "voices_file": self.tmp / "voices.json", "traces_dir": self.tmp / "traces",
                 "lane_url": "http://127.0.0.1:9", "z0live_repo": ROOT,
                 "broker_url": "http://127.0.0.1:9", "device_token_file": self.tmp / "device.token"}
        self.cfg = lambda key: str(paths[key])
        self.tools, self.commands = {}, {}
        z.register(self)

    def get_config(self, key, default=None):
        return self.cfg(key)

    def register_tool(self, name, **kw):
        self.tools[name] = kw

    def register_command(self, name, **kw):
        self.commands[name] = kw

    def call(self, name, **args):
        return json.loads(self.tools[f"z0live_{name}"]["handler"](args))

    def test_status_reports_metadata_without_transcript(self):
        (self.tmp / "lanes.json").write_text(json.dumps({"lanes": {"voice": {"actor": "grok_voice"}}}))
        (self.tmp / "voices.json").write_text(json.dumps({"guest": "grok"}))
        day = self.tmp / "traces" / "2026-11-19"
        day.mkdir(parents=True)
        (day / "call.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"wall": 100.0, "ev": "call.created", "engine": "grok"},
            {"wall": 110.0, "ev": "delegation.start", "request": "private fixture"},
            {"wall": 112.0, "ev": "guest.said"},
            {"wall": 130.0, "ev": "call.closed", "reason": "mobile_closed"}]))
        with patch.object(z, "HTTPConnection", side_effect=ConnectionRefusedError):
            out = self.call("status")
        self.assertEqual((out["engine"], out["guest"]), ("grok", "grok"))
        self.assertTrue(out["lane_server"].startswith("down"))
        self.assertEqual(out["latest_call"]["seconds"], 30.0)
        self.assertEqual(out["latest_call"]["events"], {"delegation.start": 1, "guest.said": 1})
        self.assertNotIn("private fixture", json.dumps(out))

    def test_health_distinguishes_healthy_and_legacy_servers(self):
        for code, expected in [(200, "up"), (501, "unknown (/health not supported)"), (503, "error 503")]:
            with self.subTest(code=code), patch.object(z, "HTTPConnection") as probe:
                conn = probe.return_value
                conn.getresponse.return_value.__enter__.return_value.status = code
                self.assertEqual(self.call("status")["lane_server"], expected)
                conn.request.assert_called_once_with("GET", "/health")

    def test_configure_rejects_bad_input_without_writing(self):
        for args in [{"engine": "siri"}, {"guest": "siri"}, {"tiers": None},
                     {"guest": "grok", "tiers": {"fast": [{"provider": True, "model": "x"}]}},
                     {"guest": "grok", "tiers": {"fast": [{"model": "x"}]}},
                     {"reset_tiers": True, "tiers": {"fast": [{"provider": "local", "model": "x"}]}}]:
            with self.subTest(args=args):
                self.assertIn("error", self.call("configure", **args))
        self.assertFalse((self.tmp / "voices.json").exists())
        self.assertFalse((self.tmp / "tiers.json").exists())

    def test_configure_writes_guest_and_tiers_then_resets(self):
        tiers = {"fast": [{"provider": "local", "model": "gemma4-e4b"}]}
        self.assertTrue(self.call("configure", guest="grok", tiers=tiers)["ok"])
        self.assertEqual(json.loads((self.tmp / "voices.json").read_text()), {"guest": "grok"})
        self.assertEqual(json.loads((self.tmp / "tiers.json").read_text()), tiers)
        self.assertEqual(self.call("configure", guest="off", reset_tiers=True)["applied"],
                         {"guest": "off", "tiers": "defaults"})
        self.assertFalse((self.tmp / "tiers.json").exists())

    def test_engine_tool_and_command_share_the_configured_lanes_file(self):
        (self.tmp / "traces").mkdir()
        default_keep = self.tmp / "untouched.json"
        with patch.dict(os.environ, {"Z0LIVE_LANES": str(default_keep)}):
            self.assertTrue(self.call("configure", engine="grok")["ok"])
            command = self.commands["z0live-lanes"]["handler"]
            path_before = sys.path[:]
            self.assertTrue(command("gimme 2 fast lanes, 2 small, 2 large, one council").startswith("Lanes set:"))
            board = self.call("lanes")
            self.assertEqual(sys.path, path_before)
            self.assertEqual(board["source"], self.cfg("lanes_file"))
            self.assertEqual((board["fast"], board["small"], board["large"], board["council"]), (2, 2, 2, True))
            with patch.object(z, "HTTPConnection", side_effect=ConnectionRefusedError):
                self.assertEqual(self.call("status")["engine"], "grok")
            self.assertFalse(default_keep.exists())

    def test_tools_hidden_on_fresh_checkout_and_command_does_not_write(self):
        with patch.object(z, "_secret", lambda name: ""):  # no token in the active profile
            self.assertTrue(all(not tool["check_fn"]() for tool in self.tools.values()))
        command = self.commands["z0live-lanes"]["handler"]
        self.assertIn("not configured", command("gimme 2 small lanes"))
        self.assertFalse((self.tmp / "lanes.json").exists())
        (self.tmp / "traces").mkdir()
        self.assertTrue(self.tools["z0live_lanes"]["check_fn"]())

    def test_registrations_match_manifest(self):
        manifest = (ROOT / "plugin.yaml").read_text().splitlines()
        tools = next(line.split(":", 1)[1] for line in manifest if line.startswith("provides_tools:"))
        self.assertEqual(set(self.tools), set(json.loads(tools)))
        self.assertEqual(list(self.commands), ["z0live-lanes"])

    def test_globe_needs_a_token_and_forwards_to_the_broker_command_route(self):
        with patch.object(z, "_secret", lambda name: ""):
            self.assertIn("no z0live device token", self.call("globe", action="fly-to")["error"])
            self.assertIn("args must be an object", self.call("globe", action="fly-to", args="Tokyo")["error"])
            (self.tmp / "device.token").write_text("file-token\n")
            displays = z._desktop_module("displays")
            with patch.object(displays, "client_call", return_value={"ok": True, "status": "done"}) as call:
                out = self.call("globe", action="fly-to", args={"query": "Tokyo"})
            self.assertEqual(out, {"ok": True, "status": "done"})
            call.assert_called_once_with("/v1/displays/gev/command", {"action": "fly-to", "args": {"query": "Tokyo"}},
                                         token="file-token", base="http://127.0.0.1:9")
            with patch.object(displays, "client_call", return_value={"connected": False}) as call:
                self.assertEqual(self.call("globe"), {"connected": False})
            call.assert_called_once_with("/v1/displays/gev", token="file-token", base="http://127.0.0.1:9")
            # A real (closed) port: honest unavailability, no raise, no token echo.
            out = self.call("globe", action="fly-to", args={"query": "Tokyo"})
            self.assertFalse(out["ok"])
            self.assertNotIn("file-token", json.dumps(out))

    def test_say_rejects_private_free_text_even_without_the_adapter(self):
        with patch.object(z, "HTTPConnection") as connection:
            self.assertIn("error", self.call("say", text="private fixture"))
            connection.assert_not_called()

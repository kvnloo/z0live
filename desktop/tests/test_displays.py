"""Display bridge (displays.py): registry, staleness, mint tools, command round trip, timeout, auth."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store
from z0live_desktop.displays import Displays, DisplayError
from z0live_desktop.server import Server, Handler

TOOLS = [
    {"name": "fly_to", "description": "Fly the camera to a named place"},
    {"name": "toggle_layer", "description": "Turn a data layer on or off"},
]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.d = Displays(clock=self.clock, stale=15, timeout=0.3)

    def register(self, device="dev-a", **extra):
        return self.d.register(device, {"display": "gev", "tools": TOOLS, "context": {"camera": {"lat": 0}}, **extra})

    def test_register_validates_and_lists_actions(self):
        out = self.register()
        self.assertTrue(out["ok"])
        status = self.d.status("gev")
        self.assertTrue(status["connected"])
        self.assertEqual(status["actions"], ["fly_to", "toggle_layer"])
        self.assertEqual(status["context"], {"camera": {"lat": 0}})
        for bad in (
            {"display": "GEV!", "tools": TOOLS},
            {"display": "gev", "tools": []},
            {"display": "gev", "tools": [{"name": "fly to"}]},
            {"display": "gev", "tools": [TOOLS[0], TOOLS[0]]},
            {"display": "gev", "tools": TOOLS, "context": "x"},
        ):
            with self.assertRaises(DisplayError):
                self.d.register("dev-a", bad)

    def test_staleness_after_15_seconds_without_heartbeat(self):
        self.register()
        self.clock.now += 14
        self.assertIsNotNone(self.d.alive("gev"))
        self.d.heartbeat("dev-a", "gev", {"context": {"camera": {"lat": 35.7}}})
        self.clock.now += 14
        self.assertEqual(self.d.status("gev")["context"], {"camera": {"lat": 35.7}})
        self.clock.now += 1.5
        self.assertIsNone(self.d.alive("gev"))
        self.assertEqual(self.d.status("gev"), {"display": "gev", "connected": False})
        self.assertEqual(self.d.mint_tools(), [])
        out = self.d.command("gev", "fly_to", {"query": "Tokyo"})
        self.assertEqual((out["ok"], out["status"]), (False, "no_display"))

    def test_other_device_cannot_drive_display_side_or_steal_a_live_display(self):
        self.register()
        with self.assertRaises(LookupError):
            self.d.heartbeat("dev-b", "gev")
        with self.assertRaises(LookupError):
            self.d.next_commands("dev-b", "gev")
        with self.assertRaises(LookupError):
            self.d.result("dev-b", "gev", {"id": "x", "ok": True})
        with self.assertRaises(DisplayError):
            self.register("dev-b")
        self.clock.now += 16  # stale: another paired device may take over
        self.register("dev-b")
        self.assertEqual(self.d.entries["gev"]["device"], "dev-b")

    def test_mint_tools_only_when_alive_with_enum_of_actions(self):
        self.assertEqual(self.d.mint_tools(), [])
        self.register()
        tools = self.d.mint_tools()
        self.assertEqual([t["name"] for t in tools], ["globe", "globe_context"])
        self.assertEqual(tools[0]["parameters"]["properties"]["action"]["enum"], ["fly_to", "toggle_layer"])
        self.assertIn("fly_to: Fly the camera", tools[0]["description"])

    def answer(self, ok=True, output="moved", device="dev-a"):
        """Play the display: wait for one command and answer it."""
        got = {}

        def run():
            cmds = []
            for _ in range(40):
                cmds = self.d.next_commands(device, "gev", wait=0.05)
                if cmds:
                    break
            got["cmds"] = cmds
            if cmds:
                got["reply"] = self.d.result(device, "gev", {"id": cmds[0]["id"], "ok": ok, "output": output})

        t = threading.Thread(target=run)
        t.start()
        return t, got

    def test_round_trip_success_and_failure_are_reported_honestly(self):
        self.d.clock = time.monotonic  # real waits from here on
        self.register()
        t, got = self.answer(output={"camera": {"lat": 35.68, "lon": 139.76}})
        out = self.d.tool("globe", {"action": "fly_to", "args": {"query": "Tokyo"}})
        t.join()
        self.assertEqual(got["cmds"][0]["action"], "fly_to")
        self.assertEqual(got["cmds"][0]["args"], {"query": "Tokyo"})
        self.assertEqual(got["reply"]["accepted"], True)
        self.assertEqual((out["ok"], out["status"]), (True, "done"))
        self.assertEqual(out["output"]["camera"]["lat"], 35.68)
        t, got = self.answer(ok=False, output="Refused: needs confirmation")
        out = self.d.tool("globe", {"action": "toggle_layer", "args": {"layer": "flights"}})
        t.join()
        self.assertEqual((out["ok"], out["status"], out["output"]), (False, "failed", "Refused: needs confirmation"))

    def test_timeout_is_honest_and_late_result_is_not_accepted(self):
        self.d.clock = time.monotonic
        self.register()
        start = time.monotonic()
        out = self.d.command("gev", "fly_to", {"query": "Tokyo"})
        self.assertGreaterEqual(time.monotonic() - start, 0.29)
        self.assertEqual((out["ok"], out["status"]), (False, "timeout"))
        self.assertIn("may or may not", out["output"])
        # The expired command is never delivered late, and a late result is not accepted.
        self.assertEqual(self.d.next_commands("dev-a", "gev"), [])
        self.assertFalse(self.d.result("dev-a", "gev", {"id": out["id"], "ok": True})["accepted"])

    def test_unknown_action_and_bad_args(self):
        self.register()
        out = self.d.command("gev", "launch_missiles", {})
        self.assertEqual((out["ok"], out["status"]), (False, "unknown_action"))
        with self.assertRaises(DisplayError):
            self.d.tool("globe", {"action": "fly_to", "args": "Tokyo"})
        with self.assertRaises(DisplayError):
            self.d.tool("globe", {"verb": "fly_to"})
        with self.assertRaises(DisplayError):
            self.d.tool("globe_context", {"x": 1})


class BrokerDisplayTests(unittest.TestCase):
    def setUp(self):
        selected = patch("z0live_desktop.voice_runtime.load_plan", return_value=None)
        selected.start()
        self.addCleanup(selected.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.displays.timeout = 2.0
        self.token = self.store.pair("gev display")
        self.device = self.store.authenticate(self.token)
        self.other_token = self.store.pair("phone")
        self.other = self.store.authenticate(self.other_token)

    def mint(self, device=None):
        captured = {}

        def fake(context, tools, display_tools=()):
            captured["display_tools"] = list(display_tools)
            return {"value": "explicitly-mocked-secret", "expires_at": 1}

        with patch("z0live_desktop.core.mint", side_effect=fake):
            out = self.store.new_session(device or self.other, {"client": "web", "tools": []})
        return out, captured

    def test_mint_adds_globe_only_while_display_alive(self):
        out, captured = self.mint()
        self.assertNotIn("globe", out["builtin_tools"])
        self.assertEqual(captured["display_tools"], [])
        self.store.displays.register(self.device, {"display": "gev", "tools": TOOLS})
        out, captured = self.mint()
        self.assertEqual(out["builtin_tools"][-2:], ["globe", "globe_context"])
        self.assertEqual([t["name"] for t in captured["display_tools"]], ["globe", "globe_context"])
        self.store.displays.entries["gev"]["seen"] -= 16
        out, captured = self.mint()
        self.assertNotIn("globe", out["builtin_tools"])

    def test_app_tools_cannot_claim_globe_names(self):
        with patch("z0live_desktop.core.mint", return_value={"value": "x", "expires_at": 1}):
            with self.assertRaises(ValueError):
                self.store.new_session(self.other, {"client": "web", "tools": [
                    {"type": "function", "name": "globe", "parameters": {"type": "object"}}]})

    def test_mint_persona_lists_globe_only_when_available(self):
        from z0live_desktop import core

        seen = []

        def fake_request(url, body, headers=None, timeout=25):
            seen.append(body["session"])
            return {"value": "explicitly-mocked-secret", "expires_at": 1}

        auth = Path(self.tmp.name) / "auth.json"
        import base64
        claims = base64.urlsafe_b64encode(json.dumps(
            {"https://api.openai.com/auth": {"chatgpt_account_id": "acct"}}).encode()).decode().rstrip("=")
        auth.write_text(json.dumps({"tokens": {"access_token": "h." + claims + ".s"}}))
        with patch.dict(os.environ, {"Z0LIVE_CODEX_AUTH": str(auth)}), \
                patch("z0live_desktop.core.request_json", side_effect=fake_request):
            core.mint({}, [])
            core.mint({}, [], self.store.displays.mint_tools() or Displays().mint_tools())
            self.store.displays.register(self.device, {"display": "gev", "tools": TOOLS})
            core.mint({}, [], self.store.displays.mint_tools())
        self.assertNotIn('"name":"globe"', seen[0]["instructions"])
        self.assertNotIn("globe display is connected", seen[0]["instructions"])
        self.assertNotIn("globe", [t["name"] for t in seen[0]["tools"]])
        self.assertNotIn("globe", [t["name"] for t in seen[1]["tools"]])
        self.assertIn('"name":"globe"', seen[2]["instructions"])
        self.assertIn("globe display is connected", seen[2]["instructions"])
        self.assertIn("globe", [t["name"] for t in seen[2]["tools"]])

    def start(self):
        server = Server(("127.0.0.1", 0), Handler)
        server.store = self.store
        server.origins = {"http://localhost:4173"}
        server.stop = threading.Event()
        server.sse_lock = threading.Lock()
        server.sse_clients = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.stop.set()
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)
        return "http://127.0.0.1:" + str(server.server_port)

    def req(self, base, path, body=None, token=None, method=None):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + token} if token else {})},
            method=method,
        )
        return urllib.request.urlopen(request, timeout=10)

    def test_http_round_trip_over_sse_inbox_via_phone_tool_route(self):
        base = self.start()
        with self.req(base, "/v1/displays/register",
                      {"display": "gev", "tools": TOOLS, "context": {"camera": {"lat": 0}}}, self.token) as r:
            self.assertTrue(json.load(r)["ok"])
        out, _ = self.mint()
        sid = out["session_id"]
        received = {}

        def display():
            req = urllib.request.Request(base + "/v1/displays/gev/inbox",
                                         headers={"Authorization": "Bearer " + self.token,
                                                  "Origin": "http://localhost:4173"})
            with urllib.request.urlopen(req, timeout=10) as stream:
                received["cors"] = stream.headers.get("Access-Control-Allow-Origin")
                received["type"] = stream.headers.get_content_type()
                event = {}
                for raw in stream:
                    line = raw.decode().rstrip("\n")
                    if line.startswith("event: "):
                        event["event"] = line[7:]
                    elif line.startswith("data: "):
                        event["data"] = json.loads(line[6:])
                    elif not line and "data" in event:
                        break
            received["event"] = event
            cmd = event["data"]
            with self.req(base, "/v1/displays/gev/result",
                          {"id": cmd["id"], "ok": True, "output": {"camera": {"lat": 35.68}}}, self.token) as r:
                received["ack"] = json.load(r)

        t = threading.Thread(target=display)
        t.start()
        time.sleep(0.2)
        with self.req(base, "/v1/tools/globe",
                      {"session_id": sid, "arguments": {"action": "fly_to", "args": {"query": "Tokyo"}}},
                      self.other_token) as r:
            result = json.load(r)
        t.join(5)
        self.assertEqual(received["type"], "text/event-stream")
        self.assertEqual(received["cors"], "http://localhost:4173")
        self.assertEqual(received["event"]["event"], "command")
        self.assertEqual(received["event"]["data"]["action"], "fly_to")
        self.assertEqual(received["event"]["data"]["args"], {"query": "Tokyo"})
        self.assertTrue(received["ack"]["accepted"])
        self.assertEqual((result["ok"], result["status"]), (True, "done"))
        self.assertEqual(result["output"], {"camera": {"lat": 35.68}})
        with self.req(base, "/v1/tools/globe_context", {"session_id": sid, "arguments": {}}, self.other_token) as r:
            ctx = json.load(r)
        self.assertTrue(ctx["connected"])
        self.assertEqual(ctx["context"], {"camera": {"lat": 0}})

    def test_agent_command_route_times_out_honestly(self):
        self.store.displays.timeout = 0.4
        base = self.start()
        with self.req(base, "/v1/displays/register", {"display": "gev", "tools": TOOLS}, self.token):
            pass
        with self.req(base, "/v1/displays/gev/command", {"action": "fly_to", "args": {"query": "Tokyo"}},
                      self.other_token) as r:
            out = json.load(r)
        self.assertEqual((out["ok"], out["status"]), (False, "timeout"))
        with self.req(base, "/v1/displays/gev", token=self.other_token) as r:
            self.assertTrue(json.load(r)["connected"])

    def test_auth_refusals(self):
        base = self.start()
        cases = [
            ("/v1/displays/register", {"display": "gev", "tools": TOOLS}, None, 401),
            ("/v1/displays/gev/command", {"action": "fly_to"}, "not-a-token", 401),
        ]
        for path, body, token, code in cases:
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.req(base, path, body, token)
            self.assertEqual(err.exception.code, code)
            err.exception.close()
        with self.req(base, "/v1/displays/register", {"display": "gev", "tools": TOOLS}, self.token):
            pass
        # Only the registering device may read the inbox, heartbeat or answer.
        for path, body in (("/v1/displays/gev/inbox", None), ("/v1/displays/gev/heartbeat", {}),
                           ("/v1/displays/gev/result", {"id": "x", "ok": True})):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.req(base, path, body, self.other_token)
            self.assertEqual(err.exception.code, 404)
            err.exception.close()
        # A phone tool call needs a session owned by the caller.
        out, _ = self.mint()
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.req(base, "/v1/tools/globe",
                     {"session_id": out["session_id"], "arguments": {"action": "fly_to"}}, self.token)
        self.assertEqual(err.exception.code, 404)
        err.exception.close()

    def test_voice_scope_may_call_globe_but_not_register_displays(self):
        from z0live_desktop.identity_voice import voice_route

        self.assertTrue(voice_route("POST", "/v1/tools/globe"))
        self.assertTrue(voice_route("POST", "/v1/tools/globe_context"))
        self.assertFalse(voice_route("POST", "/v1/displays/register"))
        self.assertFalse(voice_route("GET", "/v1/displays/gev/inbox"))
        self.assertFalse(voice_route("POST", "/v1/displays/gev/command"))


if __name__ == "__main__":
    unittest.main()

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import mobile_live as m


class FakeTV(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/now"):
            body = {"show": {"view": "factory", "args": {}}}
        else:
            body = {"items": [{"k": "rect"}, {"k": "text", "s": "12 agents live\nacross 4 bots"},
                              {"k": "text", "s": "RESURFACE"}]}
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)


class TvScreen(unittest.TestCase):
    def test_reads_the_view_and_its_words(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeTV)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old, m.TV_URL = m.TV_URL, f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            s = m.tv_screen()
        finally:
            m.TV_URL = old; srv.shutdown()
        self.assertIn("'factory' view", s)
        self.assertIn("12 agents live across 4 bots | RESURFACE", s)

    def test_tv_down_is_empty_not_an_error(self):
        old, m.TV_URL = m.TV_URL, "http://127.0.0.1:9"
        try:
            self.assertEqual(m.tv_screen(timeout=0.3), "")
        finally:
            m.TV_URL = old

    def test_fast_system_knows_the_tv(self):
        self.assertIn("z0 lanes", m.FAST_SYSTEM)


class LivePicture(unittest.TestCase):
    def test_never_blocks_and_refreshes_in_the_background(self):
        import threading, time
        gate = threading.Event()
        old_tv, old_facts, old_live = m.tv_screen, m.tiers.live_facts, dict(m._LIVE)
        m.tv_screen = lambda t=0.8: (gate.wait(2), "TV words")[1]
        m.tiers.live_facts = lambda: "Live lanes"
        m._LIVE.update(at=0.0, text="", busy=False)
        try:
            t = time.monotonic()
            self.assertEqual(m.live_picture(), "")  # cold: returns at once, refresh starts
            self.assertLess(time.monotonic() - t, 0.05)
            gate.set()
            for _ in range(100):
                if m._LIVE["text"]:
                    break
                time.sleep(0.01)
            self.assertEqual(m.live_picture(), "TV words\n\nLive lanes")
        finally:
            m.tv_screen, m.tiers.live_facts = old_tv, old_facts
            m._LIVE.clear(); m._LIVE.update(old_live)


class FastLaneAsk(unittest.IsolatedAsyncioTestCase):
    async def test_ask_carries_the_call_so_far_and_the_tv(self):
        seen = []

        class H:
            async def ask(self, text):
                seen.append(text); return "It's the factory view."

        class P:
            exchanges = [("user", "show me the factory"), ("assistant", "On the TV now.")]

        call = m.Call.__new__(m.Call)
        call.haiku, call.protocol = H(), P()
        call.trace = lambda *a, **k: None
        call.say_first = lambda did, text, **k: False
        old = m.tv_screen
        m.tv_screen = lambda: "Kevin's 85-inch TV is showing the z0 lanes 'factory' view right now."
        try:
            await call.ttft_lane("d1", "what am I looking at")
        finally:
            m.tv_screen = old
        self.assertEqual(len(seen), 1)
        self.assertIn("Kevin: show me the factory", seen[0])
        self.assertIn("'factory' view", seen[0])
        self.assertTrue(seen[0].rstrip().endswith("Kevin now asks: what am I looking at"))


if __name__ == "__main__":
    unittest.main()

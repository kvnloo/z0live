import json
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

# The adapter venv has no Hermes runtime. Stub its import, not the lane HTTP path.
with patch.dict("sys.modules", {"hermes_bootstrap": ModuleType("hermes_bootstrap"),
                                "agent": ModuleType("agent"),
                                "agent.auxiliary_client": SimpleNamespace(call_llm=Mock())}):
    import lane_server


@contextmanager
def serve(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


class LaneServerTest(unittest.TestCase):
    def local_request(self, events, status=200):
        requests = []

        class Model(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(b"data: " + event + b"\n\n")

            def log_message(self, format, *args):
                pass

        with serve(Model) as model_port, serve(lane_server.Handler) as lane_port, \
                patch.object(lane_server, "LOCAL_BASE_URL", f"http://127.0.0.1:{model_port}/v1/"), \
                patch.object(lane_server, "call_llm", side_effect=AssertionError("local request reached Hermes")):
            conn = HTTPConnection("127.0.0.1", lane_port, timeout=5)
            try:
                conn.request("POST", "/v1/ask", json.dumps({"provider": "local", "model": "gemma4-e4b",
                             "system": "Keep it short.", "user": "Say hello.", "max_tokens": 17, "timeout": 2}))
                with conn.getresponse() as resp:
                    return resp.status, json.load(resp), requests
            finally:
                conn.close()

    def test_local_stream_measures_first_visible_text(self):
        events = [b'{"choices":[]}', b'{"choices":[{"delta":{"role":"assistant"}}]}',
                  b'{"choices":[{"delta":{"reasoning_content":"hidden","content":" "}}]}',
                  b'{"choices":[{"delta":{"content":"Hello"}}]}',
                  b'{"choices":[{"delta":{"content":"  there."}}]}', b'[DONE]']
        # One tick for request start, one for the first visible text, one at stream end.
        with patch.object(lane_server, "time", SimpleNamespace(monotonic=Mock(side_effect=[10, 10.04, 10.12]))):
            status, out, requests = self.local_request(events)
        self.assertEqual(status, 200)
        self.assertEqual(out, {"text": "Hello there.", "first_s": 0.04, "total_s": 0.12})
        self.assertEqual(requests, [("/v1/chat/completions", {
            "model": "gemma4-e4b", "messages": [{"role": "system", "content": "Keep it short."},
                                                 {"role": "user", "content": "Say hello."}],
            "max_tokens": 17, "stream": True, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": True,
        })])

    def test_local_http_error_is_not_an_answer(self):
        status, out, _ = self.local_request([], status=503)
        self.assertEqual(status, 502)
        self.assertIn("503", out["error"])

    def test_local_truncated_stream_is_not_an_answer(self):
        status, out, _ = self.local_request([b'{"choices":[{"delta":{"content":"Partial"}}]}'])
        self.assertEqual(status, 502)
        self.assertNotIn("text", out)

    def test_hosted_stream_and_whole_reply_still_work(self):
        piece = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello"))])
        whole = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Hello"))])
        for result in (iter([piece]), whole):
            with self.subTest(result=result), patch.object(lane_server, "call_llm", return_value=result) as hosted:
                out = lane_server.ask("hosted", "model", "", "Hi", 10, 2.0)
                self.assertEqual(out["text"], "Hello")
                self.assertLessEqual(out["first_s"], out["total_s"])
                hosted.assert_called_once_with(provider="hosted", model="model", max_tokens=10, stream=True,
                                               timeout=2.0, messages=[{"role": "user", "content": "Hi"}])

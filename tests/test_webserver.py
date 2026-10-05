import tempfile
import urllib.request
from pathlib import Path

from z0live.webserver import StaticWebServer


def test_static_web_server_serves_local_bundle():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "index.html").write_text("z0live-web-ok", encoding="utf-8")
        server = StaticWebServer(root, host="127.0.0.1", port=0)
        host, port = server.start()
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as response:
                assert response.read().decode() == "z0live-web-ok"
        finally:
            server.stop()

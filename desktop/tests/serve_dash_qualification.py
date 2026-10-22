#!/usr/bin/env python3
"""Private, disposable tailnet broker for MBP synthetic-media tests. Not production."""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "desktop"))
from z0live_desktop.core import Store, dumps  # noqa: E402
from z0live_desktop.server import Server, Handler  # noqa: E402
from z0live_desktop.worker import Worker  # noqa: E402

host = os.environ["Z0LIVE_TEST_HOST"]
if ipaddress.ip_address(host) not in ipaddress.ip_network("100.64.0.0/10"):
    raise SystemExit("test listener must be an explicit tailnet IPv4 address")
data = Path(os.environ["Z0LIVE_DATA"]).resolve()
deployed = Path(os.environ["Z0LIVE_DEPLOYED_DATA"]).resolve()
if data == deployed:
    raise SystemExit("refusing deployed data directory")
os.umask(0o077)
data.mkdir(parents=True, exist_ok=True, mode=0o700)
shutil.copyfile(deployed / ".hermes-api-key", data / ".hermes-api-key")
(data / ".hermes-api-key").chmod(0o600)
os.environ["Z0LIVE_HERMES_NOTES"] = "off"
os.environ["Z0LIVE_IDENTITY"] = "0"
os.environ["Z0LIVE_DELEGATE_SESSION"] = "z0live-dash-native-qualification"
os.environ["Z0LIVE_DELEGATE_MODEL"] = "gpt-6-luna"
os.environ["Z0LIVE_DELEGATE_PROVIDER"] = "openai-codex"
store = Store(data)
with store.connect() as db:
    for name in ("initialized", "work_tools_v1"):
        db.execute("INSERT OR IGNORE INTO meta VALUES(?, 'true')", (name,))
    db.execute("INSERT OR REPLACE INTO meta VALUES('hermes_session', ?)", (dumps("z0live-dash-stack-qualification"),))
token = store.pair("dash-native-synthetic-qualification")
input_path = Path(os.environ["Z0LIVE_TEST_CLIENT_INPUT"])
input_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd = os.open(input_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump({"token": token, "target": json.loads(Path(os.environ["Z0LIVE_TEST_TARGET"]).read_text()), "remote": f"http://{host}:{os.environ['Z0LIVE_PORT']}"}, stream)
stop = threading.Event()
server = Server((host, int(os.environ["Z0LIVE_PORT"])), Handler)
server.store = store
server.stop = stop
server.origins = {"http://127.0.0.1:18791"}
server.sse_clients = 0
server.sse_lock = threading.Lock()
worker = threading.Thread(target=Worker(store, stop).run, daemon=True)
worker.start()

def end(*_):
    stop.set()
    threading.Thread(target=server.shutdown, daemon=True).start()

signal.signal(signal.SIGTERM, end)
signal.signal(signal.SIGINT, end)
print("Isolated authenticated qualification broker ready; no credential output.", flush=True)
try:
    server.serve_forever(poll_interval=0.5)
finally:
    stop.set()
    store.revoke_token(token)
    server.server_close()

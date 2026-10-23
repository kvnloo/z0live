"""Opt-in real HTTPS inbox framing/revocation check; no provider call or phone action."""

import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import BASE, ROOT, Store

store = Store()
token = store.pair("headless-inbox-" + str(time.time_ns()))
device = store.authenticate(token)
assert device is not None


def request(after):
    req = urllib.request.Request(
        BASE + "/v1/inbox?after=" + str(after),
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        assert response.headers.get_content_type() == "application/json"
        return json.load(response)


try:
    # Target this test device only; this is transport evidence, not a Hermes reply.
    message_id = store.publish("Z0LIVE_INBOX_CONTRACT", device=device)
    assert message_id is not None
    result = request(message_id - 1)
    expected = {
        "messages": [
            {
                "id": message_id,
                "say": "",
                "action": None,
                "params": {
                    "silent": True,
                    "suppressed": "stale_session",
                    "historical_say": "Z0LIVE_INBOX_CONTRACT",
                },
                "session_id": None,
            }
        ],
        "after": message_id,
        "has_more": False,
    }
    assert result == expected
    print("PASS real HTTPS inbox object", json.dumps(result))
    empty = request(message_id)
    assert empty == {"messages": [], "after": message_id, "has_more": False}
    print("PASS real HTTPS empty inbox", json.dumps(empty))
finally:
    subprocess.run(
        [str(ROOT / "bin/z0live"), "revoke", device],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    assert store.authenticate(token) is None

try:
    request(message_id)
    raise AssertionError("revoked token still authenticated")
except urllib.error.HTTPError as exc:
    assert exc.code == 401
    exc.close()
    print("PASS revoked device rejected HTTP 401")

import os
import socket
import sys
import tempfile

from z0live.plan import validate_plan
from z0live.session import BrainstormSession


def free_adjacent_ports():
    for _ in range(100):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p >= 65534:
            continue
        t = socket.socket()
        try:
            t.bind(("127.0.0.1", p + 1))
        except OSError:
            t.close()
            continue
        t.close()
        return p
    raise RuntimeError("no adjacent ports")


def test_fake_actor_is_released_on_idle_timeout():
    port = free_adjacent_ports()
    raw = {
        "schema": "z0int.voice_plan.v1",
        "plan_id": "vp_test",
        "actor_id": "personaplex-7b-nf4",
        "adapter": "personaplex",
        "device": {"index": 0},
        "admission": {"admitted": True, "status": "admitted"},
        "resource": {"residency": "session", "idle_unload_seconds": 1},
        "endpoint": {"host": "127.0.0.1", "port": port},
    }
    plan = validate_plan(raw)
    cmd = f"{sys.executable} -m z0live.fake_actor --host {{host}} --port {{port}}"
    old = os.environ.get("Z0LIVE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["Z0LIVE_HOME"] = tmp
        try:
            session = BrainstormSession(
                plan,
                actor_command=cmd,
                ready_timeout_seconds=5,
                poll_seconds=0.05,
                proxy_activity_threshold_bytes=64,
            )
            reason = session.run_foreground()
            assert reason == "idle_timeout"
            assert not session.state_path.exists()
            assert session.actor.poll() is not None
        finally:
            if old is None:
                os.environ.pop("Z0LIVE_HOME", None)
            else:
                os.environ["Z0LIVE_HOME"] = old

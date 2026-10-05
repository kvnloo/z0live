import asyncio
import os
import tempfile

from z0live.gateway import GatewayAddress
from z0live.plan import validate_plan
from z0live.service import LiveService


def test_fake_service_releases_on_idle_and_writes_receipt():
    async def go():
        plan = validate_plan(
            {
                "schema": "z0int.voice_plan.v1",
                "plan_id": "vp_service",
                "actor_id": "fake",
                "adapter": "fake",
                "provider": "local",
                "admission": {
                    "admitted": True,
                    "status": "admitted",
                },
                "resource": {
                    "residency": "session",
                    "idle_unload_seconds": 1,
                },
            }
        )
        service = LiveService(
            plan,
            listen=GatewayAddress(
                "127.0.0.1",
                0,
            ),
        )
        reason = await service.run()
        assert reason == "idle_timeout"
        assert not service.state_path.exists()
        text = service.receipts.path.read_text()
        assert "session_ready" in text
        assert "session_stopped" in text

    old = os.environ.get("Z0LIVE_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["Z0LIVE_HOME"] = tmp
        try:
            asyncio.run(go())
        finally:
            if old is None:
                os.environ.pop(
                    "Z0LIVE_HOME",
                    None,
                )
            else:
                os.environ["Z0LIVE_HOME"] = old

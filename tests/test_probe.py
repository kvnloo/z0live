import asyncio

from z0live.plan import validate_plan
from z0live.probe import probe_actor


def test_fake_actor_warm_probe_is_headless_and_releases_cleanly():
    plan = validate_plan(
        {
            "schema": "z0int.voice_plan.v1",
            "plan_id": "vp_probe",
            "actor_id": "fake",
            "adapter": "fake",
            "provider": "local",
            "model": "fake",
            "admission": {"admitted": True, "status": "admitted"},
            "resource": {"residency": "session"},
        }
    )
    result = asyncio.run(probe_actor(plan, settle_seconds=0))
    assert result["ok"] is True
    assert result["actor_id"] == "fake"
    assert result["actor_handshake_ms"] is not None
    assert result["process_ready_ms"] is None
    assert result["process_stop"] is None

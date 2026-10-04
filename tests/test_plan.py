import pytest

from z0live.plan import validate_plan


def base_plan():
    return {
        "schema": "z0int.voice_plan.v1",
        "plan_id": "vp_test",
        "actor_id": "personaplex-7b-nf4",
        "adapter": "personaplex",
        "admission": {"admitted": True, "status": "admitted"},
        "resource": {"residency": "session", "idle_unload_seconds": 120},
        "endpoint": {"host": "127.0.0.1", "port": 8998},
    }


def test_accepts_admitted_session_plan():
    plan = validate_plan(base_plan())
    assert plan.idle_unload_seconds == 120
    assert plan.endpoint_port == 8998


def test_rejects_busy_plan_with_reclaim_hint():
    raw = base_plan()
    raw["admission"] = {
        "admitted": False,
        "status": "blocked_busy",
        "reclaim_needed_mb": 2500,
    }
    with pytest.raises(ValueError, match="reclaim_needed_mb=2500"):
        validate_plan(raw)

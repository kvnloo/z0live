import pytest

from z0live.plan import validate_plan
from z0live.registry import create_actor


def base(adapter="fake"):
    return {
        "schema": "z0int.voice_plan.v1",
        "plan_id": "vp_test",
        "actor_id": "actor",
        "adapter": adapter,
        "provider": "local",
        "admission": {
            "admitted": True,
            "status": "admitted",
        },
        "resource": {
            "residency": "session",
        },
    }


def test_registry_instantiates_selected_adapter_without_ranking():
    actor = create_actor(
        validate_plan(base("fake"))
    )
    assert actor.actor_id == "actor"


def test_registry_rejects_unknown_adapter():
    with pytest.raises(
        ValueError,
        match="unknown ConversationActor",
    ):
        create_actor(
            validate_plan(base("mystery"))
        )

from __future__ import annotations

from .contracts import ConversationActor
from .plan import VoicePlan


def create_actor(plan: VoicePlan) -> ConversationActor:
    """Instantiate exactly the adapter selected by VoicePlan. No ranking here."""
    adapter = plan.adapter.lower()
    if adapter in ("personaplex", "moshi"):
        from .actors.personaplex import PersonaPlexActor

        return PersonaPlexActor(plan)
    if adapter in ("openai_realtime", "openai-realtime"):
        from .actors.openai_realtime import OpenAIRealtimeActor

        return OpenAIRealtimeActor(plan)
    if adapter == "fake":
        from .actors.fake import FakeActor

        return FakeActor(plan.actor_id)
    raise ValueError(f"unknown ConversationActor adapter selected by VoicePlan: {plan.adapter!r}")

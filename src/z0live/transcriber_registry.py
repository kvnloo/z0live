from __future__ import annotations

from .contracts import InputTranscriber
from .plan import VoicePlan


def create_transcriber(plan: VoicePlan) -> InputTranscriber | None:
    config = plan.transcriber
    if not config:
        return None

    adapter = str(config.get("adapter") or "").lower()
    if adapter == "fake":
        from .transcribers.fake import FakeTranscriber

        return FakeTranscriber(
            str(config.get("id") or "fake-transcriber")
        )

    if adapter in (
        "parakeet_eou",
        "parakeet-realtime-eou",
        "parakeet_realtime_eou_120m",
    ):
        from .transcribers.parakeet import ParakeetEOUTranscriber

        return ParakeetEOUTranscriber(
            model=str(
                config.get("model")
                or "nvidia/parakeet_realtime_eou_120m-v1"
            ),
            device=str(config.get("device") or "cuda"),
            sample_rate_hz=int(config.get("sample_rate_hz") or 16000),
            chunk_ms=int(config.get("chunk_ms") or 80),
            max_queue_chunks=int(config.get("max_queue_chunks") or 64),
        )

    raise ValueError(
        f"unknown input transcriber adapter selected by VoicePlan: {adapter!r}"
    )

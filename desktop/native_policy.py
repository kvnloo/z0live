"""Dedicated-host policy only; model selection remains the caller's pinned VoicePlan."""


class PinnedVoiceRuns:
    def _create_agent(self, *args, **kwargs):
        # Use the native adapter's existing fail-closed lock path. Never change a
        # global Hermes configuration or allow an implicit fallback behind a pinned role.
        if kwargs.get("requested_model") and kwargs.get("requested_provider"):
            kwargs["confirmed_runtime_lock"] = True
        return super()._create_agent(*args, **kwargs)

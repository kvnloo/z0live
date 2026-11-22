"""In-memory approval controls for the isolated call prototype.

Only trusted host code may open a prompt or bind the local owner mic. Neither
operation is wired to provider events, mobile messages, or production calls.
"""
from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

_WORDS = tuple("acorn almond anchor apple atlas bagel bamboo basil berry birch "
               "breeze brook cactus candle canoe cedar cherry cloud comet coral "
               "daisy delta fern fiddle finch flame forest frost garden ginger "
               "guitar harbor hazel honey iris island jasmine kettle kiwi lantern "
               "lemon lilac lotus maple marble meadow melon mint moon moss olive "
               "opal orchid otter peach pebble pine plum raven robin shell spruce "
               "tulip violet walnut willow wren".split())
PENDING_NOTICE = "Approval pending: harmless demo. Use the private approval prompt."


@dataclass(eq=False)
class _Prompt:
    request_id: str
    phrase: str = field(repr=False)
    approve_once: Callable[[str], bool] = field(repr=False)
    deadline: float
    timer: asyncio.TimerHandle = field(repr=False)
    attempts_left: int = 3


class CallApprovals:
    """One voice prompt per call, serialized on the call's asyncio loop."""

    def __init__(self, *, owner_mic: object, send_notice: Callable[[str], None],
                 show_private: Callable[[str | None], None]):
        self._owner_mic = owner_mic
        self._send_notice = send_notice
        self._show_private = show_private
        self._pending: _Prompt | None = None
        self._loop = asyncio.get_running_loop()
        self._closed = False

    def open_demo(self, request_id: str, approve_once: Callable[[str], bool], timeout_s: float) -> bool:
        """Open only the synthetic test approval. No generic command can enter here."""
        if self._closed or self._pending is not None or not request_id or not 0 < timeout_s < float("inf"):
            return False
        phrase = " ".join(secrets.choice(_WORDS) for _ in range(6))
        deadline = self._loop.time() + min(timeout_s, 30)
        timer = self._loop.call_at(deadline, self.settle, request_id)
        prompt = self._pending = _Prompt(request_id, phrase, approve_once, deadline, timer)
        try:
            self._send_notice(PENDING_NOTICE)
            self._show_private(phrase)
        except Exception:
            self._clear(prompt)
            raise
        return True

    def bind_capture(self, request_id: str, source: object) -> Callable[[str], bool] | None:
        """Bind at mic capture start, not when a delayed final transcript arrives."""
        prompt = self._pending
        if (source is not self._owner_mic or prompt is None or prompt.request_id != request_id
                or self._loop.time() >= prompt.deadline):
            return None
        return lambda text: self._answer(prompt, text)

    def _answer(self, prompt: _Prompt, text: str) -> bool:
        if self._pending is not prompt or self._loop.time() >= prompt.deadline:
            return False
        candidate = " ".join(text.lower().split()) if len(text) <= 128 and text.isascii() else ""
        if not secrets.compare_digest(candidate, prompt.phrase):
            prompt.attempts_left -= 1
            if not prompt.attempts_left:
                self._clear(prompt)
            return False
        # Consume before resolving. If another surface won, the exact-ID resolver returns false.
        self._clear(prompt)
        return bool(prompt.approve_once(prompt.request_id))

    def _clear(self, prompt: _Prompt) -> None:
        self._pending = None
        prompt.timer.cancel()
        prompt.phrase = ""
        self._show_private(None)

    def settle(self, request_id: str) -> None:
        prompt = self._pending
        if prompt is not None and prompt.request_id == request_id:
            self._clear(prompt)

    def close(self) -> None:
        self._closed = True
        if self._pending is not None:
            self._clear(self._pending)

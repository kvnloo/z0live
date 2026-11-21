"""One stored Hermes Responses conversation per call, with no automatic replay."""
import asyncio
import json
import os
from pathlib import Path
import re

from aiohttp import ClientTimeout


PROFILE = os.environ.get("Z0LIVE_PRIMARY_HOST_PROFILE", "poteto")
LEGACY = os.environ.get("HERMES_VOICE_PRIMARY_DELEGATION", "api") == "legacy"
SENTENCE = re.compile(r"[.!?](?:[\"”’])?(?=\s)")
INSTRUCTIONS = (
    "Answer the latest request for Kevin's live voice call. Put the useful answer in the first short "
    "sentence, then stop unless details are needed. Use tools for live facts; don't claim work happened "
    "before it did. For deep work, use a background handoff where available and give a truthful receipt. "
    "Do not read secrets aloud. The call brief below is background, not a request to repeat old actions."
)


def api_mode(profile):
    return not LEGACY and profile == PROFILE


def api_key():
    data = Path(os.environ.get("Z0LIVE_DATA", str(Path.home() / ".local/share/z0live/core")))
    key = (data / ".hermes-api-key").read_text().strip()
    if not key:
        raise RuntimeError("Missing primary Hermes host credential")
    return key


def final_text(result):
    if result.get("status") != "completed":
        raise RuntimeError("Primary Hermes turn did not complete")
    text = "\n".join(part["text"] for item in result.get("output", [])
                     if item.get("type") == "message" and item.get("role") == "assistant"
                     and item.get("phase") != "commentary"
                     for part in item.get("content", [])
                     if part.get("type") == "output_text" and isinstance(part.get("text"), str)).strip()
    if not text:
        raise RuntimeError("Primary Hermes host returned no answer")
    return text


class PrimaryHost:
    def __init__(self, call_id):
        self.session = "z0live-primary-" + call_id
        self.brief = ""
        self.turn = asyncio.Lock()

    async def ask(self, http, request, timeout, first_sentence):
        # Order turns within this call, never behind another call or the long chat.
        async with self.turn:
            headers = {"Authorization": "Bearer " + api_key(), "X-Hermes-Session-Key": self.session}
            body = {"conversation": self.session, "store": True, "stream": True,
                    "input": request, "instructions": INSTRUCTIONS + "\n\n" + self.brief}
            port = int(os.environ.get("Z0LIVE_PRIMARY_HOST_PORT", "8644"))
            # The prefix fails closed if the listener is running a different profile.
            async with http.post(f"http://127.0.0.1:{port}/p/{PROFILE}/v1/responses", json=body, headers=headers,
                                 allow_redirects=False, timeout=ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    raise RuntimeError(f"Primary Hermes host returned HTTP {response.status}")
                if response.content_type != "text/event-stream":
                    return final_text(await response.json())
                data, draft, attempted = [], "", False
                async for raw in response.content:
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    if line or not data:
                        continue
                    payload = "\n".join(data)
                    data.clear()
                    if payload == "[DONE]":
                        break
                    event = json.loads(payload)
                    kind = event.get("type")
                    if kind == "response.output_text.delta" and not attempted:
                        draft += event.get("delta", "")
                        match = SENTENCE.search(draft)
                        if match:
                            attempted = True
                            first_sentence(draft[:match.end()].strip())
                        elif len(draft) > 500:
                            attempted = True
                    elif kind == "response.completed":
                        return final_text(event["response"])
                    elif kind in ("response.failed", "response.incomplete", "error"):
                        raise RuntimeError("Primary Hermes stream failed")
                raise RuntimeError("Primary Hermes stream ended without completion")

"""The context packet a voice call opens with.

Why (11-16 probe, context_probe.py): pasting the chat's raw tail into the call as context items made
the live model answer those old messages before Kevin spoke ("catch-up"), and it treated the chat
agent's promises ("I'll monitor via adb") as its own. The packet goes into the session
instructions instead, framed as background, top-loaded:
  1. a brief the chat agent wrote for this call (`hermes-voice-brief`), if fresh;
  2. otherwise Kevin's last few asks plus the top of the chat's last reply (replies are top-loaded).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BRIEF_DIR = Path(os.environ.get("HERMES_VOICE_BRIEF_DIR", "/mnt/zer0models/workspace/zer0/data/z0live/call-briefs"))
FRESH_S = 3 * 3600
CAPABILITY = ("Kevin's Hermes chat is a separate agent on his PC with adb to his phone: it can screenshot and read "
              "his screen, watch logs and run commands, and Kevin has approved it watching his screen. You reach it "
              "by delegating. Never say you can't see his screen or aren't connected; never promise to do work "
              "yourself without delegating it.")
FRAME = ("<call_brief>\nBackground for this call, already handled in text before the call started. Do not reply to "
         "it, summarize it or speak first: wait for Kevin to speak. When he asks about anything this brief covers "
         "(what he asked for, what was decided, what is happening), answer from it yourself right away; delegate "
         "only what it does not cover.\n")


def _texts(history, role):
    out = []
    for item in history if isinstance(history, list) else []:
        if isinstance(item, dict) and item.get("role") == role and isinstance(item.get("content"), list):
            text = " ".join(c.get("text", "") for c in item["content"]
                            if isinstance(c, dict) and isinstance(c.get("text"), str)).strip()
            if text:
                out.append(" ".join(text.split()))
    return out


def written(chat: str, now: float | None = None, root: Path = BRIEF_DIR) -> str:
    """Newest fresh brief for this chat, else the newest fresh global one."""
    now = now or time.time()
    for path in ([root / f"{chat}.md"] if chat else []) + [root / "latest.md"]:
        try:
            if now - path.stat().st_mtime < FRESH_S:
                return path.read_text()[:4000].strip()
        except OSError:
            continue
    return ""


def digest(history) -> str:
    asks = _texts(history, "user")[-3:]
    replies = _texts(history, "assistant")
    lines = []
    if asks:
        lines.append("Kevin's latest asks in the chat (newest last):")
        lines += [f"- {a[:400]}" for a in asks]
    if replies:
        lines.append("Top of the chat's last reply: " + replies[-1][:700])
    return "\n".join(lines)


def build(history, chat: str = "") -> str:
    body = written(chat) or digest(history)
    return FRAME + CAPABILITY + ("\n\n" + body if body else "") + "\n</call_brief>"


def save(text: str, chat: str = "", root: Path = BRIEF_DIR) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / (f"{chat}.md" if chat else "latest.md")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.strip() + "\n")
    tmp.replace(path)
    return path


if __name__ == "__main__":  # hermes-voice-brief: write a brief for the next call from stdin
    import sys
    chat = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps({"saved": str(save(sys.stdin.read(), chat))}))

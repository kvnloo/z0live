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
import re
import sqlite3
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


_COUNT = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_PEOPLE = re.compile(r"\b(two|three|four|five|six|[2-6])\s+(?:people|persons|humans)\b", re.I)


def people(history) -> int:
    """How many people share the phone on this call, from the chat it opened from ("This call has two people at the
    desk, Kevin and his friend Jules"). 1 unless the chat says so."""
    found = [_PEOPLE.search(text) for role in ("user", "assistant") for text in _texts(history, role)]
    counts = [_COUNT.get(m.group(1).lower()) or int(m.group(1)) for m in found if m]
    return max(counts, default=1)


CROSS_CAP = 1500
KANBAN_DB = Path(os.environ.get("HERMES_KANBAN_DB", str(Path.home() / ".hermes" / "kanban.db")))
# FTS5 terms for Kevin saying something is wrong ("I'm still so confused, bro", "spinning in circles").
# 11-28 10:06 "do u never fucking listen to a single fucking thing i ask for??" must count.
# Not "broken"/"wrong": Kevin's long asks name bugs all the time, and those matched before his real complaints.
COMPLAINT = ('confused OR confusing OR circles OR frustrat* OR annoying OR listen OR stupid OR idiot OR fuck OR '
             'fucking OR "not working" OR "still not" OR "doesn t work" OR "didn t work" OR "what happened"')
_NOTICE = re.compile(r"^\W{1,3}\s*\[")  # a bot notice relayed into the chat ("⏸ [default] @cfg Kanban ... blocked")


def _one_line(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _kevin_said(text) -> bool:
    """A message Kevin typed, not a runtime envelope ([IMPORTANT: ..., [Context from ..., [CONTEXT COMPACTION ...)."""
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.lstrip()
    return not text.startswith("[") and not _NOTICE.match(text)


def _threads(db, now: float, window: float):
    """Kevin's chats with a message from him in the window, newest first: (sid, title, last ts, last text)."""
    import chat_continuity as cc
    rows = db.execute(f"SELECT id, coalesce(title,'') FROM sessions WHERE source NOT IN {cc.NOT_A_CHAT} AND "
                      f"{cc.NOT_KEVINS} AND (coalesce(last_activity_at, started_at) > ? OR started_at > ?) "
                      "ORDER BY coalesce(last_activity_at, started_at) DESC LIMIT 200",
                      (now - window, now - 30 * 86400)).fetchall()
    out = []
    for sid, title in rows:
        msgs = db.execute("SELECT timestamp, content FROM messages WHERE session_id=? AND role='user' AND timestamp>? "
                          "AND timestamp<=? ORDER BY timestamp DESC LIMIT 8", (sid, now - window, now)).fetchall()
        said = next(((ts, c) for ts, c in msgs if _kevin_said(c)), None)
        if said:
            out.append((sid, title, said[0], said[1]))
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def _complaint(db, now: float, chats: dict, window: float):
    """Kevin's newest complaint in any of his chats in the window (FTS over the newest rows): (ts, chat title, text)."""
    if not chats:
        return None
    marks = ",".join("?" * len(chats))  # 5 ms on tek's 300k-row store (11-28)
    hits = db.execute("SELECT m.session_id, m.content, m.timestamp FROM messages_fts f JOIN messages m ON m.id=f.rowid "
                      f"WHERE messages_fts MATCH ? AND m.role='user' AND m.session_id IN ({marks}) "
                      "AND m.timestamp>? AND m.timestamp<=? ORDER BY f.rowid DESC LIMIT 20",
                      (COMPLAINT, *chats, now - window, now)).fetchall()
    return next(((ts, chats[sid], text) for sid, text, ts in hits if _kevin_said(text)), None)


def _cards(path: Path, now: float) -> list[str]:
    """Running cards, then blocked ones (waiting on Kevin first), from the kanban board, newest first."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as kb:
        running = kb.execute("SELECT title, assignee FROM tasks WHERE status='running' "
                             "ORDER BY coalesce(last_heartbeat_at, started_at, created_at) DESC LIMIT 4").fetchall()
        blocked = kb.execute("SELECT title, assignee, coalesce(block_kind,'') FROM tasks WHERE status='blocked' "
                             "AND created_at > ? ORDER BY (block_kind='needs_input') DESC, "
                             "coalesce(last_heartbeat_at, started_at, created_at) DESC", (now - 7 * 86400,)).fetchall()
    lines = []
    if running:
        lines.append("Cards running: " + "; ".join(f"{_one_line(t, 55)} (@{a})" for t, a in running))
    if blocked:
        shown = blocked[:3]
        more = f"; +{len(blocked) - 3} more blocked" if len(blocked) > 3 else ""
        lines.append("Cards blocked: " + "; ".join(
            f"{_one_line(t, 55)} (@{a}{', waiting on Kevin' if k == 'needs_input' else ''})" for t, a, k in shown)
            + more)
    return lines


def cross_thread(profile: str = "", now: float | None = None, db_path: Path | None = None,
                 kanban: Path | None = None, cap: int = CROSS_CAP, window: float = 24 * 3600) -> str:
    """A short read-only picture of Kevin's other threads for the call's opening context: his latest ask in each
    active chat, the cards running or blocked, and his last complaint. 11-27 121336 @981s: "So what happened to all
    of my asks. You kicked off lanes for them... does that mean everything is done?" The call only knew one chat.
    At most `cap` chars; each source that fails or is missing is left out. '' when there is nothing."""
    import chat_continuity as cc
    now = time.time() if now is None else now
    asks, complaint, cards = [], None, []
    try:
        with sqlite3.connect(f"file:{db_path or cc._db(profile)}?mode=ro", uri=True, timeout=1) as db:
            threads = _threads(db, now, window)
            asks = threads[:4]
            try:
                complaint = _complaint(db, now, {sid: title for sid, title, _, _ in threads}, window)
            except sqlite3.Error:  # no FTS table in this store
                complaint = None
    except sqlite3.Error:
        pass
    try:
        cards = _cards(kanban or KANBAN_DB, now)
    except sqlite3.Error:
        cards = []
    ago = lambda ts: f"{max(0, int((now - ts) // 60))} min ago" if now - ts < 5400 else f"{(now - ts) / 3600:.0f} h ago"
    lines = []
    if asks:
        lines.append("Kevin's latest ask in each of his active chats (newest first):")
        lines += [f"- {_one_line(title or sid, 40)} ({ago(ts)}): \"{_one_line(text, 110)}\"" for sid, title, ts, text in asks]
    if complaint:  # before the cards: the cap cuts from the end, and the cards are the most compressible
        ts, title, text = complaint
        lines.append(f"Kevin's last complaint ({ago(ts)}, in {_one_line(title, 40) or 'a chat'}): \"{_one_line(text, 160)}\"")
    lines += cards
    if not lines:
        return ""
    head = "Across Kevin's threads (read-only snapshot taken at call start):"
    out = head
    for line in lines:
        if len(out) + 1 + len(line) > cap:
            room = cap - len(out) - 1
            if room > 40:
                out += "\n" + line[:room - 1] + "…"
            break
        out += "\n" + line
    return out if out != head else ""


def build(history, chat: str = "", note: str | None = None, cross: str = "") -> str:
    """`note` is the written brief already read for this call (`written(chat)`), so every voice and Hermes on the
    call works from the same one. `cross` is `cross_thread()` read once at call start."""
    body = (written(chat) if note is None else note) or digest(history)
    body = "\n\n".join(p for p in (body, cross) if p)
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

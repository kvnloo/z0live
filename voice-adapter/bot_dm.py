"""Voice @-mentions: "at PC bot, list the WD drive" / "ask the PC bot to ..." on a z0live call.

In chat, @pc reaches the pc profile through Hermes Bot Mode: the active bot calls `message_agent`, whose local
transport (hermes-agent tools/bot_mode_dm.py) admits the message into pc's canonical "Bot Chat" through pc's own
running gateway authority (`bot_relay.deliver`), waits for that turn, and prints the reply. A voice call has no Bot
Chat agent to call the tool, so this module runs that same delivery runner directly:

    <hermes python> <hermes-agent>/tools/bot_mode_dm.py --run-delivery --author <json> query-file <dm file>
        --profile-home ~/.hermes/profiles/<target> <hermes> -p <target>

No new protocol: the message lands in the target's Bot Chat exactly like a message_agent DM, attributed to the
calling profile, and the runner's stdout is the target's reply (status, delivery_id, reply|error). The receipt the
target writes (runtime/bot_live_delivery/<delivery_id>.json) names the Bot Chat session that ran it.

    parse(text, profiles, me) -> (target, message) | None     pure: is this request an @-mention of a bot?
    send(target, message, sender, call, timeout) -> dict        blocking: deliver and wait for the reply

Stdlib only. Callers that must never relay (the guest profile) are refused here, not by the caller's discipline.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

HOME = Path.home()
# Profiles that may never message another bot from a call (Kevin, 11-27: the guest profile must not get this).
DENY_SENDERS = frozenset(n.strip() for n in os.environ.get("Z0LIVE_BOT_DM_DENY", "guest").split(",") if n.strip())
MESSAGE_MAX = 16000  # message_agent's own cap (MESSAGE_MAX_CHARS)
TIMEOUT_SECONDS = 600  # the call's DELEGATION_TIMEOUT_SECONDS; the runner itself gives up on a live owner at 30 min
# Spoken forms the speech model writes for a profile name. A profile's own folder name always works too.
SPOKEN = {"potato": "poteto", "tech": "tek", "teck": "tek", "tec": "tek", "config": "cfg", "zee zero": "z0",
          "z zero": "z0", "zed zero": "z0", "z-zero": "z0", "computer": "pc"}


def profiles_dir() -> Path:
    return Path(os.environ.get("Z0LIVE_BOT_DM_PROFILES") or HOME / ".hermes/profiles")


def profiles() -> list[str]:
    try:
        return sorted(p.name for p in profiles_dir().iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def _alias_pattern(alias: str) -> str:
    """A short name may be spelled out ("p c", "P.C."); a longer one may use a space or a hyphen."""
    if len(alias) <= 3 and alias.isalnum():
        return r"[\s.]*".join(re.escape(ch) for ch in alias) + r"\.?"
    return r"[\s-]*".join(re.escape(part) for part in re.split(r"[\s-]+", alias))


def _names(names: list[str]) -> dict[str, str]:
    out = {n.lower(): n for n in names}
    for spoken, name in SPOKEN.items():
        if name in names:
            out.setdefault(spoken, name)
    return out


LEAD = r"^\W*(?:(?:hey|okay|ok|so|um|uh|and|please|now|then)\W+)*"
VERB = r"(?:ask|tell|message|ping|have|get|dm|text|send|pass|forward|relay)"
TAIL = r"(?:\s*(?:,|:|-|—)\s*|\s+)(?:(?:to|that|and|saying|asking(?:\s+(?:it|him|her|them))?\s+to|for)\s+)?(?P<msg>.+)$"
PAST = re.compile(r"\b(did|have|has|had|was|were|why|when|whether|if)\b", re.I)


def parse(text: str, names: list[str] | None = None, me: str = "") -> tuple[str, str] | None:
    """(profile, message) when `text` addresses another bot: "at PC bot list the WD drive", "@pc: ...", "ask the PC
    bot to ...", "tell poteto bot that ...", "send a message to the PC bot saying ...". The bare-verb form needs the word
    "bot" or an "@", so "ask tek about it" and "what did the PC bot say" are not mentions. None otherwise."""
    words = " ".join(str(text or "").split())
    if not words:
        return None
    table = _names(profiles() if names is None else names)
    if not table:
        return None
    alt = "|".join(_alias_pattern(a) for a in sorted(table, key=len, reverse=True))
    name = rf"(?P<name>{alt})(?![\w-])"  # not \b: "P.C." ends on a dot
    to = rf"\b{VERB}\s+(?:(?:a|the)\s+)?(?:message\s+to\s+)?(?:the\s+)?"
    forms = (
        # "at PC bot, ..." / "@pc ..." at the start of the request ("bot" is possessive: never the message)
        rf"{LEAD}(?:at|@)\s*(?:the\s+)?{name}(?:\s*bot\b)?+{TAIL}",
        # "ask the PC bot to ..." / "tell poteto bot that ..."
        rf"{to}{name}\s*bot\b{TAIL}",
        # "ask @pc to ..."
        rf"{to}@\s*{name}(?:\s*bot\b)?+{TAIL}",
    )
    for form in forms:
        m = re.search(form, words, re.I)
        if not m:
            continue
        if form is not forms[0] and PAST.search(words[:m.start()]):
            continue  # "did you ask the PC bot to ...": about a mention, not one
        target = next((v for k, v in sorted(table.items(), key=lambda kv: -len(kv[0]))
                       if re.fullmatch(_alias_pattern(k), m.group("name"), re.I)), None)
        msg = m.group("msg").strip().rstrip(".?! ").strip()
        if target and msg and target != me:
            return target, msg
    return None


def hermes_python() -> str:
    """The interpreter the hermes launcher execs (the delivery runner boots Hermes deps itself)."""
    if os.environ.get("Z0LIVE_HERMES_PY"):
        return os.environ["Z0LIVE_HERMES_PY"]
    launcher = os.environ.get("HERMES_BIN") or str(HOME / ".hermes/hermes-agent/.hermes/bin/hermes")
    try:
        m = re.search(r"^exec\s+(\S+python\S*)", Path(launcher).read_text(errors="replace"), re.M)
        if m and Path(m.group(1)).exists():
            return m.group(1)
    except OSError:
        pass
    return "python3"


def runner() -> str:
    return os.environ.get("Z0LIVE_BOT_DM_RUNNER") or str(HOME / ".hermes/hermes-agent/tools/bot_mode_dm.py")


def envelope(message: str, sender: str, call: str) -> str:
    """What the target reads: message_agent's signature line, then Kevin's request as he said it on the call."""
    return (f"Message from 🤖 {sender} (@{sender}): Kevin asked for this on a z0live voice call (call {call}). "
            f"He is waiting on the call and your reply is read to him, so answer in a few short spoken sentences.\n\n"
            f"{message}")


def _receipt(home: Path, delivery_id: str) -> dict:
    try:
        return json.loads((home / "runtime/bot_live_delivery" / f"{delivery_id}.json").read_text())
    except (OSError, ValueError):
        return {}


def send(target: str, message: str, sender: str, call: str = "", timeout: float = TIMEOUT_SECONDS) -> dict:
    """Deliver into `target`'s Bot Chat and wait for its turn. Returns {ok, target, status, delivery_id, session_id,
    reply, error, s}. Never raises; a refusal or failure is ok=False with the reason."""
    began = time.monotonic()
    out = {"ok": False, "target": target, "sender": sender}
    if sender in DENY_SENDERS:
        return {**out, "error": f"the {sender} profile may not message other bots", "refused": True}
    if target not in profiles():
        return {**out, "error": f"no profile named {target}"}
    if target == sender:
        return {**out, "error": "a bot cannot message itself"}
    message = str(message or "").strip()
    if not message:
        return {**out, "error": "empty message"}
    home = profiles_dir() / target
    body = envelope(message, sender or "z0live", call)[:MESSAGE_MAX]
    author = {"id": f"bot:{sender or 'z0live'}", "name": sender or "z0live", "is_bot": True}
    tmp = Path(tempfile.mkdtemp(prefix="z0live-botdm-"))
    dm_file = tmp / "dm.txt"
    try:
        dm_file.write_text(body, encoding="utf-8")
        os.chmod(dm_file, 0o600)
        argv = [hermes_python(), runner(), "--run-delivery", "--author", json.dumps(author, separators=(",", ":")),
                "query-file", str(dm_file), "--profile-home", str(home), "hermes", "-p", target]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  cwd=str(HOME))
        except subprocess.TimeoutExpired:
            return {**out, "error": f"no reply from {target} within {int(timeout)} s; it may still answer in its Bot Chat",
                    "status": "pending", "s": round(time.monotonic() - began, 1)}
        except OSError as exc:
            return {**out, "error": f"could not start the delivery runner: {exc}"}
        line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.strip().startswith("{")), "")
        try:
            data = json.loads(line) if line else {}
        except ValueError:
            data = {}
        if not data:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
            return {**out, "error": f"delivery runner failed: {tail[0][:300]}", "s": round(time.monotonic() - began, 1)}
        did = data.get("delivery_id") or ""
        rec = _receipt(home, did) if did else {}
        status = data.get("status") or ("failed" if data.get("error") else "")
        reply = str(data.get("reply") or "").strip()
        return {**out, "ok": status == "settled" and not data.get("error"), "status": status, "delivery_id": did,
                "session_id": rec.get("session_id") or "", "reply": reply, "error": data.get("error") or "",
                "reason": data.get("reason") or "", "outcome": data.get("outcome") or "",
                "s": round(time.monotonic() - began, 1)}
    finally:
        for p in (tmp / "dm.txt.live.json", dm_file):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass


def spoken(result: dict, label: str) -> str:
    """The line the voice says for a finished delivery."""
    if result.get("ok"):
        return f"{label} says: {result.get('reply') or 'nothing (an empty reply)'}"
    if result.get("refused"):
        return "Messaging other bots isn't available on this profile."
    return f"I couldn't get an answer from {label}: {result.get('error') or result.get('status') or 'unknown error'}."


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="send one voice @-mention to another bot profile")
    ap.add_argument("--from", dest="sender", required=True)
    ap.add_argument("--call", default="cli")
    ap.add_argument("text", help='e.g. "at PC bot list the WD drive"')
    a = ap.parse_args(argv)
    hit = parse(a.text, me=a.sender)
    if not hit:
        print(json.dumps({"ok": False, "error": "not a bot mention"}))
        return 2
    res = send(hit[0], hit[1], a.sender, a.call)
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))

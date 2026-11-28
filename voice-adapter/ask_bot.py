"""Ask another Hermes bot profile: "ask @pc to list the drive" (Kevin's 11-27 call).

Kevin asks one bot (on a call or in chat) to pass a message to another profile and bring the answer back. The other
profile answers in its own Hermes host: the unsandboxed API-only host that launchd runs per profile
(desktop/hermes_host.py --profile <name>, key in ~/.config/z0live/guest-hosts/<name>.key). The asking agent never runs
`hermes -p <name>` itself: tek's agents live under the seatbelt profile, which denies writes to other profiles'
runtime dirs (the Bot Mode `hermes -p pc chat -c "Bot Chat"` transport fails there on active_sessions.lock).

    parse(text) -> (profile, message) | None   the spoken/typed command, only for profiles that have a host
    ask(profile, message, call=..., by=...)    POST, then ledger: open/claim -> receipt (kind ask, ref = the
                                               completion id) -> result (the answer) -> done. Returns {ok, say, ...}

Rules: the `guest` profile is never a target and never a caller; a message that looks like it holds a secret is
refused before it leaves; the answer is masked the same way before it is spoken or written. Stdlib only, loadable by
path (like jobs.py). CLI: python3 ask_bot.py pc "list the drive"  (also `z0live ask ...`).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
DEFAULT_HOSTS = {"pc": 18645}  # launchd com.kvn.hermes-pc: hermes_host.py --profile pc --port 18645
DENIED = {"guest"}  # a guest never reaches another bot, and no bot reaches the guest
TIMEOUT_S = 900
MAX_MESSAGE = 4000
BY = "ask"
SYSTEM = ("This message comes from Kevin's {caller} bot, relaying what Kevin asked on his {via}. Do what he asks with "
          "your own tools, then answer in a few short plain sentences: your reply is read aloud to him. Never include "
          "secrets, keys or passwords in the reply.")
SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:password|passwd|api[_ ]?key|secret|token)\s*[:=]\s*\S+"
    r"|\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[g-zG-Z])[A-Za-z0-9_-]{40,}\b)", re.I)  # a 40-hex sha is not a secret


def hosts() -> dict[str, int]:
    """Profiles that can be asked: Z0LIVE_ASK_HOSTS (json {name: port}) or ~/.config/z0live/ask-hosts.json, else pc."""
    raw = os.environ.get("Z0LIVE_ASK_HOSTS")
    if not raw:
        f = HOME / ".config/z0live/ask-hosts.json"
        raw = f.read_text() if f.is_file() else ""
    try:
        table = {str(k).lower(): int(v) for k, v in json.loads(raw).items()} if raw else dict(DEFAULT_HOSTS)
    except (ValueError, AttributeError, TypeError):
        table = dict(DEFAULT_HOSTS)
    return {k: v for k, v in table.items() if k not in DENIED and re.fullmatch(r"[a-z0-9_-]+", k)}


def caller_profile() -> str:
    home = os.environ.get("HERMES_HOME") or ""
    return Path(home).name if "/profiles/" in home else ("default" if home else "")


# ---- parse ----
_LEAD = r"^\W*(?:(?:hey|okay|ok|so|and|um|uh|please|can you|could you|would you|will you|go ahead and)\W+)*"


def _names() -> str:
    """Host profile names as ASR may write them: "pc", "PC", "p c", "p.c."."""
    return "|".join(r"[\s.]*".join(map(re.escape, n)) + r"\.?" for n in sorted(hosts(), key=len, reverse=True)) or "(?!)"


def _name(at: bool) -> str:
    lead = r"(?:@\s*|at\s+)" if at else r"(?:the\s+)?(?:@\s*|at\s+)?"
    # "PCBot": the speech model glues the suffix on (seen in the 11-27 real-voice probe), so allow it with no space.
    return (lead + r"(?P<p>" + _names() + r")"
            r"(?:\s*(?:bot|profile|agent)(?![a-z0-9_-])|(?![a-z0-9_-]))")


def _profile(word: str) -> str | None:
    word = re.sub(r"[\s.]", "", word.lower())
    return word if word in hosts() else None


def parse(text: str) -> tuple[str, str] | None:
    """("pc", "list the drive") for "ask @pc to list the drive", "ask the PC bot: list the drive", "@pc list the
    drive", "at pc, list the drive", "tell pc to list the drive". Only a profile with a host counts, so "ask the team
    to ..." or "ask him ..." are not commands."""
    t = " ".join(str(text or "").split())
    if not t:
        return None
    forms = (
        _LEAD + r"(?:ask|tell|message|ping)\s+" + _name(False) + r"\s*(?:,|:|-|\s)\s*(?:to\s+)?(?P<m>.+)$",
        _LEAD + _name(True) + r"\s*[,:-]?\s+(?P<m>.+)$",
        # ASR hears "At PC bot," as "That PC bot," (11-28 real-voice probe). Only with "bot" and a , or : after it,
        # so "that PC bot is slow" stays a remark, not an ask.
        _LEAD + r"that\s+(?P<p>" + _names() + r")\s*bot\s*[,:]\s*(?P<m>.+)$",
    )
    for form in forms:
        m = re.match(form, t, re.I)
        if not m:
            continue
        profile = _profile(m.group("p"))
        message = m.group("m").strip(" ,:-")
        if profile and len(message) >= 2:
            return profile, message[:MAX_MESSAGE]
    return None


def mask(text: str) -> str:
    return SECRET.sub("[redacted]", str(text or ""))


# ---- ledger (path-loaded; optional, same contract as jobs.py) ----
_ledger = None


def ledger():
    global _ledger
    if _ledger is None:
        for p in (os.environ.get("Z0LIVE_LEDGER_PY"), str(HERE / "ledger.py")):
            if p and Path(p).is_file():
                try:
                    spec = importlib.util.spec_from_file_location("_z0live_ask_ledger", p)
                    assert spec and spec.loader
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    _ledger = mod
                    break
                except Exception:  # noqa: BLE001
                    return None
    return _ledger


def _led(fn, *a, **kw):
    mod = ledger()
    if mod is None or not (a and a[0] or fn == "open_item"):
        return None, "ledger module not found"
    try:
        return getattr(mod, fn)(*a, **kw), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"


# ---- transport ----
def _post(profile: str, port: int, session: str, messages: list, timeout: float) -> tuple[str, str, str]:
    """(answer, completion id, session id echoed). Raises RuntimeError with a short reason."""
    key_file = HOME / ".config/z0live/guest-hosts" / f"{profile}.key"
    try:
        key = key_file.read_text().strip()
    except OSError:
        raise RuntimeError(f"no host key for {profile}") from None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", method="POST",
        data=json.dumps({"model": "z0live", "messages": messages}).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Hermes-Session-Id": session})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - loopback only
            body = json.loads(r.read().decode())
            echoed = r.headers.get("X-Hermes-Session-Id") or ""
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{profile}'s host returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"{profile}'s host is not reachable ({type(exc).__name__})") from None
    except ValueError:
        raise RuntimeError(f"{profile}'s host returned non-JSON") from None
    try:
        answer = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        answer = ""
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError(f"{profile} returned no answer")
    return answer.strip(), str(body.get("id") or ""), echoed


def _run(profile: str, port: int, session: str, messages: list, timeout: float, on_approval) -> tuple[str, str, str]:
    """N230: the same ask as a Runs API run, so a Hermes approval prompt reaches the caller (on_approval) instead of
    being denied for having nobody to answer. (answer, run id, session). Raises RuntimeError with a short reason."""
    import voice_approvals  # sibling module
    client = voice_approvals.RunsClient.for_profile(profile, port)
    try:
        run_id, output, failed = voice_approvals.run(
            client, messages[-1]["content"], session, messages[0]["content"],
            on_approval, timeout)
    except RuntimeError as exc:
        raise RuntimeError(f"{profile}'s {exc}") from None
    if failed and not output.strip():
        raise RuntimeError(f"{profile}'s run ended without an answer ({failed[:120]})")
    if not output.strip():
        raise RuntimeError(f"{profile} returned no answer")
    return output.strip(), run_id, session


def ask(profile: str, message: str, call: str = "", item: str = "", by: str = BY, via: str = "chat",
        caller: str = "", timeout: float = TIMEOUT_S, on_approval=None) -> dict:
    """Send message to profile's Hermes host and wait for its answer. Never raises. ok=True only with a delivery id.
    on_approval(run_id, event): when given, the ask runs on the host's Runs API and each approval event is
    handed to it without blocking the run reader."""
    profile = (profile or "").strip().lstrip("@").lower()
    message = " ".join(str(message or "").split())[:MAX_MESSAGE]
    caller = caller or caller_profile() or "tek"
    if caller in DENIED:
        return {"ok": False, "error": "the guest profile cannot message other bots"}
    if profile in DENIED:
        return {"ok": False, "error": "the guest profile cannot be messaged"}
    table = hosts()
    if profile not in table:
        return {"ok": False, "error": f"no bot host for {profile or '(none)'}; can ask: {', '.join(sorted(table)) or 'none'}"}
    if not message:
        return {"ok": False, "error": "message is empty"}
    if SECRET.search(message):
        return {"ok": False, "error": "the message looks like it holds a secret; not sent"}
    led_err = None
    if not item:
        opened, led_err = _led("open_item", call or None, f"ask @{profile}: {message}", "action", "background")
        item = str(opened or "")
    led_ok = bool(item) and not led_err
    if led_ok:
        _led("claim", item, by, f"asking {profile}")
    session = f"z0live-ask-{caller}-{call or time.strftime('%Y%m%d')}"[:120]
    messages = [{"role": "system", "content": SYSTEM.format(caller=caller, via="voice call" if via == "voice" else via)},
                {"role": "user", "content": message}]
    began = time.monotonic()
    try:
        if on_approval is not None:
            answer, cid, echoed = _run(profile, table[profile], session, messages, timeout, on_approval)
        else:
            answer, cid, echoed = _post(profile, table[profile], session, messages, timeout)
    except RuntimeError as exc:
        if led_ok:
            _led("result", item, by, f"ask @{profile} failed: {exc}", False)
        return {"ok": False, "error": str(exc), "item": item, "say": f"I couldn't reach {profile}: {exc}."}
    answer = mask(answer)
    ref = f"{profile}:{cid or echoed}"
    if led_ok:
        _, e = _led("receipt", item, by, "ask", ref, f"session {echoed or session}")
        _, e2 = _led("result", item, by, f"{profile}: {answer}", True, [ref])
        led_err = e or e2
    out = {"ok": True, "profile": profile, "answer": answer, "delivery": {"id": cid, "session": echoed or session},
           "item": item, "ledger": led_ok and not led_err, "s": round(time.monotonic() - began, 1),
           "say": f"{profile} says: {answer}"}
    if led_err:
        out["ledger_error"] = led_err
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="z0live ask", description="ask another Hermes bot profile")
    ap.add_argument("profile", help="profile to ask (e.g. pc), or the whole command with --parse")
    ap.add_argument("message", nargs="*")
    ap.add_argument("--call", default="")
    ap.add_argument("--parse", action="store_true", help="treat the words as a spoken command ('ask @pc to ...')")
    a = ap.parse_args(argv)
    words = " ".join([a.profile, *a.message])
    if a.parse:
        hit = parse(words)
        if not hit:
            print(json.dumps({"ok": False, "error": "not an ask command"}))
            return 1
        out = ask(*hit, call=a.call, by="cli")
    else:
        out = ask(a.profile, " ".join(a.message), call=a.call, by="cli")
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

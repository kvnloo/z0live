"""z0live jobs: ask -> durable owned job -> proof -> reported back (Kevin's 11-27 call).

A job is a long piece of work (a hill climb, a sweep, a build) that must keep running after the call ends and after the
agent that started it is gone. The executor is the Hermes kanban board of profile `tek`: the host gateway's dispatcher
claims a ready task within about a minute, runs a full agent worker in an isolated workspace, and records its summary.
A second engine, `pid`, runs a plain shell command under nohup with a pid file for work that needs no agent.

    start(brief, ...)   create the job, write a ledger receipt (kind kanban|pid, ref = the id). Idempotent per ledger item.
    sync(id)            fold the executor's state into the ledger: running (only once a worker really claimed it), then
                        result() + done|blocked|failed|dropped. Idempotent: a second sync appends nothing.
    status/list/stop    read (they call sync first) and end a job.

Stdlib only. The ledger is loaded by path (ledger.py next to this file, or Z0LIVE_LEDGER_PY); if it is missing or a
ledger write fails the job still starts and the result says `ledger: false`, never a crash. Ledger state is the contract:
nobody says "started" without the receipt `start` returns.

CLI: python3 jobs.py start|status|list|stop|sync (also `z0live job ...`). Tests set Z0LIVE_JOBS, Z0LIVE_LEDGER and a
fake hermes in Z0LIVE_HERMES; no real board is touched.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
HERMES_HOME_DEFAULT = HOME / ".hermes/profiles/tek"
ASSIGNEE = "tek"
# Another Hermes profile's gateway can run the job instead of tek ("@pc bot, check the drive"): its kanban dispatcher claims
# tasks assigned to it and runs them with that bot's tools and outside tek's sandbox (pc on 11-28). guest never gets jobs.
NO_BOT = {"guest"}


def bot_ok(bot: str) -> str:
    """'' if `bot` may own a job, else why not."""
    if not bot or bot == ASSIGNEE:
        return ""
    if bot in NO_BOT or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", bot):
        return f"bot {bot!r} cannot take jobs"
    if not (HOME / ".hermes/profiles" / bot / "config.yaml").is_file():
        return f"no Hermes profile named {bot!r}"
    return ""
BY = "job"
ENGINES = ("kanban", "pid")
MAX_BRIEF = 20000
STUCK_S = 300  # a ready task the dispatcher has not claimed in this long is stuck
# kanban status -> what the job is. ready/todo/triage = waiting for a worker; the dispatcher claims within ~1 min.
RUNNING = {"running"}
DONE = {"done"}
BLOCKED = {"blocked", "review"}
DROPPED = {"archived"}

WORKER_RULES = (
    "\n\n---\nJOB RULES (z0live): this is a durable job started from a voice call. Work unattended, never ask questions; "
    "if a decision is missing, choose the safest reading and say so. Use only the newest models; never service_tier or "
    "ultrafast. Commit work to a feature branch, never main. Private call words never go into git. When finished, "
    "complete the task and make the summary the answer Kevin should hear: what shipped, exact paths/commits/ids, "
    "numbers before/after, what is left. If you cannot finish, block the task with the reason in one sentence."
)


def _env(name: str, default):
    return os.environ.get(name) or default


def jobs_dir() -> Path:
    return Path(_env("Z0LIVE_JOBS", HOME / ".local/share/z0live/jobs"))


def hermes_bin() -> str:
    return _env("Z0LIVE_HERMES", None) or shutil.which("hermes") or str(HOME / ".local/bin/hermes")


def hermes_home() -> str:
    return _env("Z0LIVE_JOB_HERMES_HOME", str(HERMES_HOME_DEFAULT))


def now() -> float:
    return time.time()


# ---- ledger (path-loaded; optional) ----
_ledger = None


def ledger():
    """The shared request ledger module, or None when it is not there. Never raises."""
    global _ledger
    if _ledger is not None:
        return _ledger
    for p in (os.environ.get("Z0LIVE_LEDGER_PY"), str(HERE / "ledger.py")):
        if p and Path(p).is_file():
            try:
                spec = importlib.util.spec_from_file_location("_z0live_ledger", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _ledger = mod
                return mod
            except Exception:  # noqa: BLE001
                return None
    return None


def _led(fn, *a, **kw):
    """Call a ledger function; (value, error). A ledger problem is reported, never raised."""
    mod = ledger()
    if mod is None:
        return None, "ledger module not found"
    try:
        return getattr(mod, fn)(*a, **kw), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"


# ---- job records ----
def _job_dir(jid: str) -> Path:
    if not jid or "/" in jid or jid.startswith("."):
        raise ValueError("bad job id")
    return jobs_dir() / jid


def _read(jid: str) -> dict | None:
    try:
        return json.loads((_job_dir(jid) / "job.json").read_text())
    except (OSError, ValueError):
        return None


def _write(job: dict) -> None:
    d = _job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=1, sort_keys=True))
    os.replace(tmp, d / "job.json")


class _Lock:
    """One flock for all job bookkeeping: start/sync/stop never interleave (the TV, the CLI and a tool may race)."""

    def __enter__(self):
        jobs_dir().mkdir(parents=True, exist_ok=True)
        self.f = open(jobs_dir() / ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _run(argv, timeout=60, env=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:  # no hermes binary, or it hung: a failed call, not a crash
        return 127, "", f"{type(exc).__name__}: {str(exc)[:120]}"
    return p.returncode, p.stdout, p.stderr


def _hermes(args, timeout=60):
    env = {**os.environ, "HERMES_HOME": hermes_home()}
    return _run([hermes_bin(), *args], timeout=timeout, env=env)


def _kanban_json(args) -> tuple[dict | None, str]:
    rc, out, err = _hermes(["kanban", *args, "--json"])
    if rc != 0:
        return None, (err or out).strip()[:200]
    try:
        return json.loads(out), ""
    except ValueError:
        return None, "kanban returned non-JSON"


# ---- start ----
def _key(brief: str, item: str | None) -> str:
    return "z0live-" + (item or hashlib.sha1(brief.encode()).hexdigest()[:16])


def start(brief: str, title: str = "", engine: str = "kanban", model: str = "", provider: str = "", item: str = "",
          call: str = "", cmd: str = "", workspace: str = "", max_runtime: str = "8h", goal: bool = True,
          by: str = BY, bot: str = "") -> dict:
    """Start a job. Returns {ok, id, item, engine, receipt, ledger, ...}. Safe to call twice with the same item/brief:
    the second call returns the first job (kanban idempotency key) instead of starting another."""
    brief = str(brief or "").strip()
    if not brief:
        return {"ok": False, "error": "brief is empty"}
    if engine not in ENGINES:
        return {"ok": False, "error": f"engine must be one of {ENGINES}"}
    if engine == "pid" and not cmd:
        return {"ok": False, "error": "engine pid needs cmd"}
    if provider and not model:
        return {"ok": False, "error": "provider needs model"}
    if "ultrafast" in (model + provider).lower():
        return {"ok": False, "error": "ultrafast is not allowed"}
    bot = (bot or "").strip().lstrip("@").lower()
    why = bot_ok(bot)
    if why:
        return {"ok": False, "error": why}
    if bot and engine != "kanban":
        return {"ok": False, "error": "a bot job runs on the kanban board (engine kanban)"}
    if bot and bot != ASSIGNEE and (model or provider):  # cfg: pinning another bot's model/provider is a spend change
        return {"ok": False, "error": "a job for another bot runs on that bot's own model; omit model/provider"}
    brief = brief[:MAX_BRIEF]
    title = (title or brief.split("\n", 1)[0])[:90]
    led_err = None
    with _Lock():
        # dedupe first, on a key that exists before any ledger write: the same ask said twice is one job
        key = _key(brief, item or None) + (f"-{bot}" if bot and bot != ASSIGNEE else "")
        for j in _all_jobs():
            if j.get("key") == key and j.get("state") not in ("dropped", "failed"):
                return {"ok": True, "id": j["id"], "item": j.get("item"), "engine": j["engine"], "again": True,
                        "state": j.get("state"), "receipt": j.get("receipt"), "ledger": j.get("ledger", False)}
        if item:
            for j in _all_jobs():
                if j.get("item") == item and j.get("state") not in ("dropped", "failed"):
                    return {"ok": True, "id": j["id"], "item": item, "engine": j["engine"], "again": True,
                            "state": j.get("state"), "receipt": j.get("receipt"), "ledger": j.get("ledger", False)}
        if item and ledger() is not None:  # the ledger drops events for ids it never opened: refuse a made-up id
            known, _e = _led("get", item)
            if known is None and not _e:
                return {"ok": False, "error": f"no ledger item {item}; omit item to open one"}
        if not item and call:  # the voice adapter already opened this ask's item: put the proof on it, not a new one
            mod = ledger()
            try:
                mine = [it for it in (mod.items(call=call, open_only=True) if mod else [])
                        if it.get("kind") == "action" and not it.get("proven")]
            except Exception:  # noqa: BLE001
                mine = []
            item = mine[0]["id"] if mine else ""
        if not item:
            item, led_err = _led("open_item", call or None, title, "action", "job")
        led_ok = bool(item) and not led_err
        if led_ok:
            _, e = _led("claim", item, by, f"job: {title}")
            led_err = led_err or e
        if engine == "kanban":
            body = brief
            if bot and bot != ASSIGNEE:  # cfg 11-28: a card handed to another bot names who asked, and keeps Kevin's gate
                body = (f"Requested from z0live by {ASSIGNEE} ({by}) on call {call or 'none'} for Kevin.\n"
                        "Config, secret or spending changes still need Kevin's explicit go: block this card with the "
                        "question for him; do not assume this card is that go.\n\n" + brief)
            jid, ref_err = _start_kanban(body, title, model, provider, workspace, max_runtime, goal, key, by,
                                         bot or ASSIGNEE)
        else:
            jid, ref_err = _start_pid(brief, title, cmd, workspace, item)
        if ref_err:  # nothing started: the claim stays honest ("claimed, not started") and we say why
            if led_ok:
                _led("state", item, "failed", by, f"job did not start: {ref_err}")
            return {"ok": False, "error": ref_err, "item": item}
        job = {"id": jid, "engine": engine, "title": title, "item": item, "call": call, "model": model,
               "provider": provider, "workspace": workspace, "created": now(), "by": by, "state": "waiting",
               "synced": None, "ledger": led_ok, "receipt": None, "key": key, "bot": bot or ASSIGNEE}
        kind = engine
        ref = jid
        if engine == "pid":
            job["pid"] = int(_pidfile(jid) or 0)
            ref = str(job["pid"])
        if led_ok:
            _, e = _led("receipt", item, by, kind, ref, f"{engine} job {jid}")
            if e:
                led_err, led_ok = e, False
            job["ledger"] = led_ok
        job["receipt"] = {"kind": kind, "ref": ref}
        d = _job_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "brief.md").write_text(brief)  # private: stays on this machine, never in git
        _write(job)
    out = {"ok": True, "id": jid, "item": item, "engine": engine, "receipt": job["receipt"], "ledger": led_ok,
           "bot": bot or ASSIGNEE}
    if led_err:
        out["ledger_error"] = led_err
    return out


def _start_kanban(brief, title, model, provider, workspace, max_runtime, goal, key, by, assignee=ASSIGNEE):
    ws = workspace or "scratch"
    argv = ["kanban", "create", title, "--assignee", assignee, "--body", brief + WORKER_RULES, "--workspace", ws,
            "--max-runtime", max_runtime, "--idempotency-key", key, "--created-by", f"z0live-{by}"]
    if goal:
        argv.append("--goal")
    if model:
        argv += ["--model", model] + (["--provider", provider] if provider else [])
    data, err = _kanban_json(argv[1:])
    if data is None:
        return None, f"kanban create failed: {err}"
    jid = data.get("id") or (data.get("task") or {}).get("id")
    if not jid:
        return None, "kanban create returned no task id"
    return str(jid), ""


def _pidfile(jid):
    try:
        return (_job_dir(jid) / "pid").read_text().strip()
    except OSError:
        return ""


def _start_pid(brief, title, cmd, workspace, item):
    jid = "j-" + time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1((item or brief).encode()).hexdigest()[:6]
    d = _job_dir(jid)
    d.mkdir(parents=True, exist_ok=True)
    wrapper = f"{cmd}\necho $? > {shlex.quote(str(d / 'exit'))}\n"
    log = open(d / "log.txt", "ab")
    try:
        p = subprocess.Popen(["/bin/sh", "-c", wrapper], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             cwd=workspace or str(d), start_new_session=True)
    except OSError as exc:
        return None, f"could not start: {exc}"
    (d / "pid").write_text(str(p.pid))
    return jid, ""


# ---- sync: executor state -> ledger ----
def _all_jobs() -> list[dict]:
    out = []
    try:
        for d in sorted(jobs_dir().iterdir()):
            j = _read(d.name) if d.is_dir() else None
            if j:
                out.append(j)
    except OSError:
        pass
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _observe(job: dict) -> tuple[str, str, str]:
    """(state, text, why): what the executor says now. state in waiting|running|done|blocked|failed|dropped."""
    if job["engine"] == "pid":
        d = _job_dir(job["id"])
        pid = int(job.get("pid") or 0)
        if (d / "exit").exists():
            code = (d / "exit").read_text().strip()
            tail = ""
            try:
                tail = (d / "log.txt").read_bytes()[-1500:].decode("utf-8", "replace").strip()
            except OSError:
                pass
            return ("done" if code == "0" else "failed"), tail or f"exit {code}", f"exit {code}"
        if pid and _pid_alive(pid):
            return "running", "", ""
        return "failed", "", "process gone without an exit code"
    data, err = _kanban_json(["show", job["id"]])
    if data is None:
        return job["state"], "", f"board unreadable: {err}"
    task = data.get("task") or {}
    status = task.get("status") or ""
    summary = (data.get("latest_summary") or "").strip()
    if status in DONE:
        return "done", summary or "completed (no summary)", ""
    if status in RUNNING:
        return "running", "", ""
    if status in BLOCKED:
        comments = data.get("comments") or []
        why = (comments[-1].get("body") if comments and isinstance(comments[-1], dict) else "") or task.get("block_reason") or status
        return "blocked", "", str(why)[:200]
    if status in DROPPED:
        return "dropped", "", "archived on the board"
    return "waiting", "", status


def sync(jid: str) -> dict:
    with _Lock():
        job = _read(jid)
        if not job:
            return {"ok": False, "error": f"no job {jid}"}
        return _sync_locked(job)


def _sync_locked(job: dict) -> dict:
    if job.get("state") in ("done", "dropped", "failed") and job.get("synced") == job["state"]:
        return _view(job)
    state, text, why = _observe(job)
    if state == "waiting" and not job.get("stuck") and now() - job["created"] > STUCK_S:
        job["stuck"] = True  # nobody claimed it: say so on the ledger instead of leaving a silent promise
        if job.get("ledger") and job.get("item"):
            _led("state", job["item"], "blocked", BY, f"no worker claimed the job in {STUCK_S // 60:.0f} min ({why})")
        _write(job)
    if state == "running" and job.get("stuck"):
        job["stuck"] = False
    if state != job["state"] or (state in ("done", "blocked", "failed") and job.get("synced") != state):
        job["state"] = state
        if job.get("ledger") and job.get("item"):
            if state in ("running", "blocked", "dropped"):
                _led("state", job["item"], state, BY, why)
            elif state == "done":
                _led("result", job["item"], BY, text, True, [f"job:{job['id']}"])
                _led("state", job["item"], "done", BY, "")
            elif state == "failed":
                _led("result", job["item"], BY, text or why, False, [f"job:{job['id']}"])
                _led("state", job["item"], "failed", BY, why)
        job["synced"] = state if state in ("done", "blocked", "failed", "dropped") else job.get("synced")
        if state in ("done", "failed"):
            job["finished"] = now()
            (_job_dir(job["id"]) / "result.md").write_text(text or why or "")
        _write(job)
    return _view(job)


def _view(job: dict) -> dict:
    d = _job_dir(job["id"])
    out = {"ok": True, "id": job["id"], "engine": job["engine"], "title": job["title"], "state": job["state"],
           "item": job.get("item"), "receipt": job.get("receipt"), "ledger": job.get("ledger"),
           "age_s": round(now() - job["created"])}
    try:
        out["result"] = (d / "result.md").read_text()[:4000]
    except OSError:
        pass
    return out


def sync_all() -> list[dict]:
    """Fold every unfinished job. Cheap when nothing is open (no subprocess). Call from a tick or cron."""
    out = []
    with _Lock():
        for j in _all_jobs():
            if j.get("state") in ("done", "dropped", "failed") and j.get("synced") == j["state"]:
                continue
            out.append(_sync_locked(j))
    return out


def status(jid: str) -> dict:
    return sync(jid)


def list_jobs(open_only: bool = False) -> list[dict]:
    sync_all()
    with _Lock():
        views = [_view(j) for j in reversed(_all_jobs())]
    return [v for v in views if not open_only or v["state"] in ("waiting", "running", "blocked")]


def stop(jid: str, why: str = "stopped by request") -> dict:
    with _Lock():
        job = _read(jid)
        if not job:
            return {"ok": False, "error": f"no job {jid}"}
        if job["engine"] == "pid":
            pid = int(job.get("pid") or 0)
            if pid:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        else:  # reclaim releases the worker; block parks the card so the dispatcher does not run it again
            _hermes(["kanban", "reclaim", jid, "--reason", why])
            _hermes(["kanban", "block", jid, why])
        job["state"] = "dropped"
        job["synced"] = "dropped"
        if job.get("ledger") and job.get("item"):
            _led("state", job["item"], "dropped", BY, why)
        _write(job)
        return _view(job)


# ---- CLI ----
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="z0live job", description="durable owned jobs")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("start")
    s.add_argument("brief", nargs="?", default="", help="the work to do ('-' or empty with --brief-file)")
    s.add_argument("--brief-file")
    s.add_argument("--title", default="")
    s.add_argument("--engine", choices=ENGINES, default="kanban")
    s.add_argument("--model", default="")
    s.add_argument("--provider", default="")
    s.add_argument("--item", default="", help="ledger item id this job answers (omit to open one)")
    s.add_argument("--call", default="")
    s.add_argument("--cmd", default="", help="engine pid: the shell command to run")
    s.add_argument("--workspace", default="", help="kanban: scratch | worktree | dir:<path>")
    s.add_argument("--max-runtime", default="8h")
    s.add_argument("--no-goal", action="store_true")
    s.add_argument("--bot", default="", help="another Hermes profile runs it (e.g. pc); default tek")
    for name in ("status", "stop"):
        p = sub.add_parser(name)
        p.add_argument("id")
    sy = sub.add_parser("sync", help="fold every open job into the ledger; prints only jobs whose state changed")
    sy.add_argument("--all", action="store_true", help="print every open job, changed or not")
    l = sub.add_parser("list")
    l.add_argument("--open", action="store_true")
    a = ap.parse_args(argv)
    if a.verb == "start":
        brief = Path(a.brief_file).read_text() if a.brief_file else (sys.stdin.read() if a.brief == "-" else a.brief)
        out = start(brief, a.title, a.engine, a.model, a.provider, a.item, a.call, a.cmd, a.workspace, a.max_runtime,
                    not a.no_goal, bot=a.bot)
    elif a.verb == "status":
        out = status(a.id)
    elif a.verb == "stop":
        out = stop(a.id)
    elif a.verb == "sync":
        before = {j["id"]: j["state"] for j in _all_jobs()}
        views = sync_all()
        out = {"ok": True, "jobs": [v for v in views if a.all or before.get(v["id"]) != v["state"]]}
        if not out["jobs"] and not a.all:  # nothing changed: silent, so a no-agent cron says nothing
            return 0
    else:
        out = {"ok": True, "jobs": list_jobs(a.open)}
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

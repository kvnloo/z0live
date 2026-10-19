"""Bounded read-only work evidence. No LLM, shell interpolation or history writes."""

from __future__ import annotations

from datetime import datetime, timedelta
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from .core import ROOT, request_json


def compact(value, limit=600):
    value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Defense in depth: excerpts go to a voice model, never credentials.
    value = re.sub(
        r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,\"}]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    return value[:limit]


def run(argv, cwd: str | Path = ROOT, timeout=20, env=None):
    p = subprocess.run(
        ["nice", "-n", "19", *map(str, argv)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if p.returncode:
        raise RuntimeError(f"{Path(str(argv[0])).name} exited {p.returncode}")
    return p.stdout


def zhome():
    return Path(os.environ.get("Z0INT_HOME", str(Path.home() / ".z0int")))


def packets():
    out = []
    for path in sorted((zhome() / "state/state_packet").glob("*/latest.json"))[:200]:
        try:
            if path.stat().st_size < 2_000_000:
                packet = json.loads(path.read_text())
                repo = str(packet.get("scope", {}).get("repo", ""))
                if (
                    repo
                    and Path(repo).is_dir()
                    and not any(
                        x in repo for x in ("pytest-", "/scratch/", "/salvage/")
                    )
                ):
                    out.append((path, packet))
        except (OSError, ValueError):
            continue
    return out


def projects():
    """State Packet registry first; known live products are explicit fallbacks."""
    mapping = {
        Path(p.get("scope", {}).get("repo", "")).name: p.get("scope", {}).get("repo")
        for _, p in packets()
        if p.get("scope", {}).get("repo")
    }
    mapping.update(
        {
            "garden_room": str(ROOT.parent / "garden_room"),
            "z0live": str(ROOT),
            "lawnchair": "/mnt/zer0models/lawnchair-z0live",
            "z0intelligence": "/mnt/zer0models/z0-wt/z0intelligence",
        }
    )
    return {k: v for k, v in mapping.items() if Path(v).is_dir()}


def project_name(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("invalid project")
    key = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "garden": "garden_room",
        "gardenroom": "garden_room",
        "lawnchair_z0live": "lawnchair",
    }
    key = aliases.get(key, key)
    mapping = projects()
    match = next((k for k in mapping if k.lower().replace("-", "_") == key), None)
    if not match:
        raise ValueError("unknown project; use a registered project name")
    return match


def window(since="week"):
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if since == "today":
        return today.timestamp(), now.timestamp()
    if since == "yesterday":
        return (today - timedelta(days=1)).timestamp(), today.timestamp()
    if since == "week":
        return (now - timedelta(days=7)).timestamp(), now.timestamp()
    raise ValueError("since must be today, yesterday or week")


def in_window(value, start, end):
    try:
        stamp = (
            float(value)
            if isinstance(value, (float, int))
            else datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        )
        return start <= stamp < end
    except (TypeError, ValueError):
        return False


def databases():
    configured = os.environ.get("Z0LIVE_HISTORY_ROOTS")
    roots = (
        [Path(x).expanduser() for x in configured.split(os.pathsep)]
        if configured
        else [Path("/workspace/hermes-home"), Path.home() / ".hermes"]
    )
    found = {}
    for root in roots:
        for p in [root / "state.db", *root.glob("profiles/*/state.db")]:
            if p.is_file():
                found[str(p.resolve())] = p
    return list(found.values())


@contextmanager
def ro(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 0.5
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        yield db
    finally:
        db.close()


def terms(query):
    stop = {
        "what",
        "was",
        "working",
        "yesterday",
        "today",
        "doing",
        "worked",
        "the",
        "in",
        "on",
        "i",
        "a",
        "my",
        "work",
        "have",
        "been",
    }
    return [t for t in re.findall(r"[\w]+", query.lower()) if t not in stop][:12]


def hermes(query="", project=None, scope="recent", since=None):
    start, end = (
        window(since or "week") if scope != "all" or since else (0, time.time())
    )
    tokens = terms(project or query)
    fts = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
    out, errors = [], []
    for path in databases():
        try:
            with ro(path) as db:
                if fts:
                    rows = db.execute(
                        """SELECT m.id,m.session_id,m.timestamp,m.content,s.title,s.cwd
                        FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
                        JOIN sessions s ON s.id=m.session_id
                        WHERE messages_fts MATCH ? AND m.timestamp>=? AND m.timestamp<?
                        AND m.role='assistant' AND length(trim(coalesce(m.content,'')))>0 ORDER BY m.timestamp DESC LIMIT 12""",
                        (fts, start, end),
                    ).fetchall()
                else:
                    rows = db.execute(
                        """SELECT m.id,m.session_id,m.timestamp,m.content,s.title,s.cwd
                        FROM messages m JOIN sessions s ON s.id=m.session_id
                        WHERE m.timestamp>=? AND m.timestamp<? AND m.role='assistant' AND length(trim(coalesce(m.content,'')))>0
                        ORDER BY m.timestamp DESC LIMIT 6""",
                        (start, end),
                    ).fetchall()
                for r in rows:
                    if (
                        project
                        and project.replace("_", "").lower()
                        not in (str(r["cwd"]) + str(r["content"]))
                        .replace("_", "")
                        .lower()
                    ):
                        continue
                    out.append(
                        {
                            "source": "hermes",
                            "project": project or r["cwd"],
                            "date": datetime.fromtimestamp(r["timestamp"])
                            .astimezone()
                            .isoformat(),
                            "session": r["session_id"],
                            "what": compact(r["content"]),
                            "status": "historical statement; not live completion",
                            "citation": f"hermes:{path}#message={r['id']}",
                        }
                    )
        except sqlite3.Error as exc:
            errors.append(f"{path.parent.name}: {type(exc).__name__}")
    return {
        "items": sorted(out, key=lambda x: x["date"], reverse=True)[:12],
        "gaps": errors,
        "coverage": f"{len(databases())} read-only profile databases; assistant statements only",
    }


def z0(query, project=None, since=None, scope="recent", include_code=True, **_):
    tokens = terms(project or query)
    start, end = (
        window(since or "week") if scope != "all" or since else (0, time.time())
    )
    items, gaps = [], []
    for path, packet in packets():
        if since and not in_window(packet.get("built_at"), start, end):
            continue
        if project and packet.get("scope", {}).get("repo") != projects()[project]:
            continue
        content = json.dumps(packet, ensure_ascii=False)
        if tokens and not any(t in content.lower() for t in tokens):
            continue
        items.append(
            {
                "source": "z0intelligence-state-packet",
                "project": packet.get("scope", {}).get("repo"),
                "date": packet.get("built_at"),
                "session": None,
                "what": compact(
                    packet.get("current_claims", packet.get("decision", {})), 900
                ),
                "status": "cached State Packet; verify freshness",
                "citation": str(path),
            }
        )
    path = zhome() / "memory/events.jsonl"
    if path.is_file():
        # Tail is explicitly bounded; older recall is covered by ctx and Hermes FTS.
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 2_000_000))
            rows = f.read().splitlines()
        for raw in reversed(rows):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not start <= event.get("ts", 0) < end:
                continue
            if project and event.get("project") != projects()[project]:
                continue
            # Search-attempt receipts are not memories of completed work.
            if event.get("event_type") == "context.resolve":
                continue
            hay = json.dumps(event, ensure_ascii=False)
            if tokens and not any(t in hay.lower() for t in tokens):
                continue
            payload = event.get("payload")
            if payload is None:
                gaps.append(f"memory event {event.get('event_id')}: blob omitted")
                continue
            items.append(
                {
                    "source": "z0intelligence-memory",
                    "project": event.get("project"),
                    "date": event.get("ts"),
                    "session": event.get("session_id"),
                    "what": compact(payload),
                    "status": event.get("event_type"),
                    "citation": f"{path}#event={event.get('event_id')}",
                }
            )
            if len(items) >= 10:
                break
    if project and include_code:
        try:
            data = request_json(
                os.environ.get("Z0INT_SERVICE_URL", "http://127.0.0.1:11501")
                + "/v1/file-search",
                {
                    "harness": "hermes",
                    "repository_root": projects()[project],
                    "query": query,
                    "kind": "natural_language",
                    "limit": 4,
                    "time_budget_ms": 120,
                },
                timeout=1.2,
            )
            for e in data.get("packet", {}).get("evidence", []):
                items.append(
                    {
                        "source": "z0intelligence-code",
                        "project": project,
                        "date": e.get("observed_at"),
                        "session": None,
                        "what": compact(e.get("excerpt", "")),
                        "status": "index hit",
                        "citation": e.get("locator", e.get("source_id")),
                    }
                )
            gaps.extend(data.get("packet", {}).get("unresolved_gaps", []))
        except RuntimeError as exc:
            gaps.append(str(exc))
    return {
        "items": items[:12],
        "gaps": gaps,
        "coverage": "cached State Packets + last 2 MB of memory + resident code search",
    }


def ctx(query, project=None, scope="recent", since=None):
    query = " ".join(terms(query)) or project or query
    if project and project not in query:
        query = project
    argv = [
        os.environ.get("Z0LIVE_CTX_BIN", str(Path.home() / ".local/bin/ctx")),
        "search",
        query,
        "--data-root",
        os.environ.get("CTX_DATA_ROOT", "/mnt/zer0models/host-offload/home_kvn_.ctx"),
        "--refresh",
        "off",
        "--backend",
        "lexical",
        "--format",
        "json",
        "--content-scope",
        "transcript",
        "--event-type",
        "message",
        "--limit",
        "8",
    ]
    if project:
        argv += ["--workspace", projects()[project]]
    start, end = (0, time.time())
    if scope != "all" or since:
        start, end = window(since or "week")
        argv += ["--since", datetime.fromtimestamp(start).astimezone().isoformat()]
    data = json.loads(run(argv, timeout=75))
    items = [
        {
            "source": "ctx:" + r.get("provider", "unknown"),
            "project": project,
            "date": r.get("timestamp"),
            "session": r.get("provider_session_id", r.get("session_id")),
            "what": compact(r.get("snippet", "")),
            "status": "indexed history; not live completion",
            "citation": f"ctx:{r.get('ctx_session_id')}#{r.get('ctx_event_id')}",
        }
        for r in data.get("results", [])
        if in_window(r.get("timestamp"), start, end)
    ]
    return {"items": items, "gaps": [], "coverage": data.get("freshness", {})}


def changes(query="", project=None, since="today", **_):
    start, end = window(since)
    items, gaps = [], []
    for name, path in (
        [(project, projects()[project])] if project else list(projects().items())[:20]
    ):
        try:
            output = run(
                [
                    "git",
                    "log",
                    "--since=@" + str(int(start)),
                    "--until=@" + str(int(end)),
                    "--format=%h%x09%cI%x09%s",
                    "-12",
                ],
                cwd=path,
                timeout=3,
            )
            for line in output.splitlines():
                sha, date, subject = line.split("\t", 2)
                items.append(
                    {
                        "source": "git",
                        "project": name,
                        "date": date,
                        "session": None,
                        "what": compact(subject),
                        "status": "committed",
                        "citation": f"{path}@{sha}",
                    }
                )
            prs = json.loads(
                run(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--state",
                        "all",
                        "--limit",
                        "8",
                        "--json",
                        "number,title,state,url,updatedAt",
                    ],
                    cwd=path,
                    timeout=6,
                )
            )
            for p in prs:
                ts = datetime.fromisoformat(
                    p["updatedAt"].replace("Z", "+00:00")
                ).timestamp()
                if start <= ts <= end:
                    items.append(
                        {
                            "source": "github",
                            "project": name,
                            "date": p["updatedAt"],
                            "session": None,
                            "what": compact(p["title"]),
                            "status": p["state"],
                            "citation": p["url"],
                        }
                    )
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            gaps.append(f"{name}: {type(exc).__name__}")
    # Final local telemetry, no raw audio or arbitrary file tree scans.
    for name, path in [
        ("z0live", ROOT.parents[1] / "data/z0live/broker.db"),
        ("garden_room", ROOT.parent / "garden_room/data/voice/events.jsonl"),
    ]:
        if project and project != name:
            continue
        try:
            if name == "garden_room" and path.exists():
                with path.open("rb") as f:
                    f.seek(max(0, path.stat().st_size - 1_000_000))
                    events = []
                    for raw in f.read().splitlines():
                        try:
                            e = json.loads(raw)
                            if start <= e.get("ts", 0) < end:
                                events.append(e)
                        except (ValueError, TypeError):
                            continue
                failures = sum(e.get("success") is False for e in events)
                items.append(
                    {
                        "source": "telemetry",
                        "project": name,
                        "date": since,
                        "session": None,
                        "what": f"{len(events)} voice events; {failures} reported action failures (last 1 MB only)",
                        "status": "observed",
                        "citation": str(path),
                    }
                )
            if name == "z0live" and path.exists():
                with ro(path) as db:
                    n = db.execute(
                        "SELECT count(*) FROM events WHERE ts>=? AND ts<?", (start, end)
                    ).fetchone()[0]
                items.append(
                    {
                        "source": "telemetry",
                        "project": name,
                        "date": since,
                        "session": None,
                        "what": f"{n} voice events received",
                        "status": "observed",
                        "citation": str(path),
                    }
                )
        except sqlite3.Error:
            gaps.append(name + ": telemetry unavailable")
    return {"items": items[:40], "gaps": gaps}


def native_tasks(query="", project=None, ident=None, **_):
    items, gaps = [], []
    for path in databases():
        try:
            with ro(path) as db:
                for r in db.execute(
                    "SELECT delegation_id,origin_session,state,updated_at,task_json,result_json FROM async_delegations ORDER BY updated_at DESC LIMIT 8"
                ):
                    if (
                        ident
                        and not ident.startswith("voice-")
                        and ident != r["delegation_id"]
                    ):
                        continue
                    body = compact(r["task_json"])
                    if project and project not in body:
                        continue
                    items.append(
                        {
                            "source": "hermes-delegation",
                            "project": project,
                            "date": r["updated_at"],
                            "session": r["origin_session"],
                            "what": body,
                            "status": r["state"],
                            "citation": f"{path}#delegation={r['delegation_id']}",
                        }
                    )
        except sqlite3.Error:
            gaps.append(f"{path.parent.name}: async_delegations unavailable")
    return {"items": items[:30], "gaps": gaps}


def task_sources(query="", project=None, ident=None, **_):
    items, gaps = [], []
    for name, path in (
        [(project, projects()[project])] if project else [("z0live", str(ROOT))]
    ):
        for kind in ("pr", "issue"):
            try:
                fields = "number,title,state,url" + (
                    ",isDraft,updatedAt,statusCheckRollup" if kind == "pr" else ""
                )
                rows = json.loads(
                    run(
                        [
                            "gh",
                            kind,
                            "list",
                            "--limit",
                            "8",
                            "--state",
                            "all",
                            "--json",
                            fields,
                        ],
                        cwd=path,
                        timeout=6,
                    )
                )
                if ident and not ident.startswith("voice-"):
                    rows = [r for r in rows if ident in {str(r["number"]), r["url"]}]
                items.extend(
                    {
                        "source": "github",
                        "project": name,
                        "date": None,
                        "session": None,
                        "what": compact(r["title"]),
                        "status": r["state"] + (" draft" if r.get("isDraft") else ""),
                        "checks": [
                            {
                                "name": c.get("name"),
                                "status": c.get("status"),
                                "conclusion": c.get("conclusion"),
                            }
                            for c in r.get("statusCheckRollup") or []
                        ],
                        "citation": r["url"],
                    }
                    for r in rows
                )
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                gaps.append(name + ": GitHub " + kind + " unavailable")
    return {"items": items[:20], "gaps": gaps}


def kanban_tasks(query="", project=None, ident=None, **_):
    items, gaps = [], []
    try:
        rows = json.loads(
            run(
                [
                    str(Path.home() / ".local/bin/hermes"),
                    "kanban",
                    "list",
                    "--json",
                    "--archived",
                ],
                timeout=10,
                env={
                    "HERMES_HOME": os.environ.get(
                        "Z0LIVE_HERMES_HOME", "/workspace/hermes-home/profiles/clean"
                    )
                },
            )
        )
        for r in rows if isinstance(rows, list) else rows.get("tasks", []):
            if ident and not ident.startswith("voice-") and ident != r.get("id"):
                continue
            if project and project not in json.dumps(r):
                continue
            items.append(
                {
                    "source": "kanban",
                    "project": project,
                    "date": r.get("updated_at"),
                    "session": None,
                    "what": compact(r.get("title", r)),
                    "status": r.get("status", "unknown"),
                    "citation": "kanban:" + str(r.get("id")),
                }
            )
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        gaps.append("kanban unavailable")
    return {"items": items[:30], "gaps": gaps}

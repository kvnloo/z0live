#!/usr/bin/env python3
"""Validate synthetic public API samples and known incorrect legacy shapes."""

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = Path(__file__).with_name("sample_frames.json")
KNOWN = {
    "mint": ("POST", "/v1/realtime/session"),
    "tell": ("POST", "/v1/tools/tell_hermes"),
    "context": ("POST", "/v1/tools/get_context"),
    "summarize": ("POST", "/v1/tools/summarize"),
    "events": ("POST", "/v1/events"),
    "inbox": ("GET", "/v1/inbox?after=0"),
    "sessions": ("GET", "/v1/sessions"),
    "end": ("POST", "/v1/sessions/synthetic_session/end"),
}
TYPES = {
    "user_transcript", "assistant_transcript", "tool_call",
    "tool_result", "summary", "feedback", "app_state",
}
ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def is_object(value):
    return isinstance(value, dict)


def inbox(page):
    if not is_object(page) or set(page) != {"messages", "after", "has_more"}:
        return False
    if not isinstance(page["messages"], list) or not isinstance(page["has_more"], bool):
        return False
    if not isinstance(page["after"], int) or isinstance(page["after"], bool) or page["after"] < 0:
        return False
    last = 0
    for message in page["messages"]:
        if not is_object(message) or set(message) != {
            "id", "say", "action", "params", "session_id",
        }:
            return False
        if not isinstance(message["id"], int) or message["id"] <= last:
            return False
        if not isinstance(message["say"], str) or not is_object(message["params"]):
            return False
        if message["action"] is not None and not isinstance(message["action"], str):
            return False
        if message["session_id"] is not None and not ID.fullmatch(message["session_id"]):
            return False
        last = message["id"]
    return not page["messages"] or page["after"] == last


def events(batch):
    if not isinstance(batch, list) or not 1 <= len(batch) <= 100:
        return False
    for event in batch:
        if not is_object(event) or not ID.fullmatch(event.get("session_id", "")):
            return False
        if event.get("type") not in TYPES or not is_object(event.get("data")):
            return False
        ts = event.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return False
        if not is_object(event["data"]) or not isinstance(
            event["data"].get("event_id"), str
        ):
            return False
    return True


def validate(value, api_text):
    errors = []
    if not is_object(value) or value.get("fixture_revision") != 1:
        return ["invalid fixture revision"]
    if value.get("server_origin") != "https://broker.example.invalid":
        errors.append("non-synthetic origin")
    if "Synthetic" not in value.get("provenance", ""):
        errors.append("missing synthetic provenance")
    samples = value.get("requests")
    if not isinstance(samples, list):
        return ["requests must be an array"]
    seen = set()
    by_id = {}
    for item in samples:
        if not is_object(item) or item.get("id") not in KNOWN:
            errors.append("unknown request")
            continue
        name = item["id"]
        if name in seen:
            errors.append("duplicate sample id " + name)
        seen.add(name)
        by_id[name] = item
        if (item.get("method"), item.get("path")) != KNOWN[name]:
            errors.append("wrong route or HTTP method: " + name)
        if name == "events" and not events(item.get("body")):
            errors.append("event batch must be a bare array with unique event IDs")
        if name in {"tell", "context", "summarize"}:
            body = item.get("body")
            if not is_object(body) or not ID.fullmatch(body.get("session_id", "")):
                errors.append("invalid builtin session")
            elif not is_object(body.get("arguments")):
                errors.append("builtin missing arguments object")
    if seen != set(KNOWN):
        errors.append("missing required sample request")
    result = value.get("responses")
    if not is_object(result):
        return ["responses must be an object"]
    if not inbox(result.get("inbox")):
        errors.append("inbox must be object {messages,after,has_more}")
    minted = result.get("mint")
    if not is_object(minted) or not ID.fullmatch(minted.get("session_id", "")):
        errors.append("mint missing generated session ID")
    elif not isinstance(minted.get("expires_at"), (int, float)):
        errors.append("mint expires_at must be Unix seconds")
    for name in ("tell", "summarize"):
        response = result.get(name)
        if not is_object(response) or response.get("status") != "queued":
            errors.append(name + " must mean queued, not completed")
    response = result.get("events")
    if not is_object(response) or response.get("accepted") != len(
        by_id.get("events", {}).get("body", [])
    ):
        errors.append("accepted event count mismatch")
    bad = value.get("known_invalid", {})
    if not is_object(bad) or events(bad.get("wrapped_events")) or inbox(bad.get("bare_inbox")):
        errors.append("legacy incorrect shapes must be rejected")
    for required in (
        "POST /v1/realtime/session", "POST /v1/events",
        "GET /v1/inbox?after=N", "GET /v1/stream?after=N",
        "/v1/tools/tell_hermes", "POST /v1/sessions/SESSION_ID/end",
    ):
        if required not in api_text:
            errors.append("API.md no longer documents: " + required)
    serialized = json.dumps(value)
    if any(item in serialized for item in ("0.taila30dc.ts.net", "Authorization: Bearer", "sk-proj-")):
        errors.append("private host, credential or forbidden header fixture")
    return errors


def main():
    document = json.loads((Path(sys.argv[1]) if len(sys.argv) == 2 else SAMPLE).read_text())
    errors = validate(document, (ROOT / "docs/API.md").read_text())
    for error in errors:
        print("INVALID " + error, file=sys.stderr)
    if errors:
        return 1
    print("VALID: public synthetic HTTP example shapes (offline, no provider/device)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Print one short-lived Grok Voice client secret as JSON {"value", "expires_at"}.

Reads XAI_API_KEY only from the adapter's own service environment (`~/.config/z0live/voice.env`, loaded by
deploy/macos/run.sh) and trades it for a client secret that expires in minutes, so the realtime voice leg
never holds the long-lived key. It does not read Hermes's xAI OAuth login or any Hermes auth state.
It runs under Hermes's runtime (`hermes --print-runtime-command`) only for that runtime's `httpx`.
"""
import json
import os
import sys

sys.path.insert(0, os.environ.get("HERMES_AGENT_SOURCE", os.path.expanduser("~/.hermes/hermes-agent")))
import hermes_bootstrap  # noqa: E402,F401 - initializes the installed Hermes runtime
import httpx  # noqa: E402

key = os.environ.get("XAI_API_KEY", "").strip()
if not key:
    sys.exit("no xAI credential: set XAI_API_KEY in the voice adapter's service env (~/.config/z0live/voice.env)")
resp = httpx.post("https://api.x.ai/v1/realtime/client_secrets", timeout=15,
                  headers={"Authorization": f"Bearer {key}"},
                  json={"expires_after": {"seconds": int(os.environ.get("XAI_SECRET_SECONDS", "600"))}})
if resp.status_code != 200:
    sys.exit(f"xAI client_secrets HTTP {resp.status_code}")  # vendor bodies can carry account details
data = resp.json()
print(json.dumps({"value": data["value"], "expires_at": data.get("expires_at")}))

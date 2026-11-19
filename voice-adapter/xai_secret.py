"""Print one short-lived Grok Voice client secret as JSON {"value", "expires_at"}.

Runs under Hermes's own runtime (`hermes --print-runtime-command`), so the xAI OAuth login stays inside
Hermes (refresh included) and the voice adapter only ever holds a secret that expires in minutes.
Set up the login once with `hermes auth add xai-oauth --type oauth`. XAI_API_KEY wins when set.
"""
import json
import os
import sys

sys.path.insert(0, os.environ.get("HERMES_AGENT_SOURCE", os.path.expanduser("~/.hermes/hermes-agent")))
import hermes_bootstrap  # noqa: E402,F401 - initializes the installed Hermes runtime
import httpx  # noqa: E402

key = os.environ.get("XAI_API_KEY", "").strip()
if not key:
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials  # noqa: E402
    key = (resolve_xai_oauth_runtime_credentials() or {}).get("api_key") or ""
if not key:
    sys.exit("no xAI credential: run `hermes auth add xai-oauth --type oauth`")
resp = httpx.post("https://api.x.ai/v1/realtime/client_secrets", timeout=15,
                  headers={"Authorization": f"Bearer {key}"},
                  json={"expires_after": {"seconds": int(os.environ.get("XAI_SECRET_SECONDS", "600"))}})
if resp.status_code != 200:
    sys.exit(f"xAI client_secrets HTTP {resp.status_code}")  # vendor bodies can carry account details
data = resp.json()
print(json.dumps({"value": data["value"], "expires_at": data.get("expires_at")}))

"""Run the real Hermes queue and call transcript encoder against a synthetic request.

No agent, tool execution, network, WebRTC negotiation, or microphone is started.
The owner mic and the phone's private approval display are in-process stand-ins.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from call_approvals import CallApprovals, PENDING_NOTICE


async def probe():
    from gateway.session_pending_controls import PendingControls
    from mobile_live import Protocol
    from tools.approval import observe_gateway_settle
    from tools.approval_gateway_wait import _await_gateway_decision

    class Events:
        lock = threading.RLock()

        def publish(self, session_id, payload, *, event_type):
            events.put_nowait((session_id, event_type, payload))

    loop = asyncio.get_running_loop()
    ready = asyncio.Queue()
    events = asyncio.Queue()
    controls = PendingControls(Events())
    route, sid = uuid4().hex, uuid4().hex
    generation = 1
    phone_transcript = []
    private_display = []
    owner_mic = object()
    protocol = Protocol()

    def phone_receive(text):
        event = json.loads(json.dumps(protocol.transcript("assistant", text)))
        assert event["type"] == "session.output_transcript.delta"
        phone_transcript.append(event["delta"])

    call = CallApprovals(owner_mic=owner_mic, send_notice=phone_receive, show_private=private_display.append)

    def notify(data):
        request_id = data["request_id"]
        assert observe_gateway_settle(route, request_id, lambda _: loop.call_soon_threadsafe(call.settle, request_id))
        loop.call_soon_threadsafe(ready.put_nowait, data)

    async def wait_for_approval(label):
        return await asyncio.to_thread(_await_gateway_decision, route, notify,
                                       {"command": label, "description": "No action is executed"}, surface="test")

    def approve_once(request_id):
        return controls.respond(sid, generation, request_id, {"choice": "once"})["status"] == "resolved"

    with patch("tools.approval_context._get_approval_timeout", return_value=5), \
            patch("tools.approval_context._fire_approval_hook"):
        older = asyncio.create_task(wait_for_approval("Synthetic control request"))
        try:
            control = await asyncio.wait_for(ready.get(), 3)
            demo = asyncio.create_task(wait_for_approval("Synthetic call approval"))
            data = await asyncio.wait_for(ready.get(), 3)
            # gateway.run would launch Hermes' dependency bootstrap on import.
            # Use the same redactor without importing the service entry point.
            from agent.redact import redact_approval_display
            redactor = SimpleNamespace(_redact_approval_command=lambda text: redact_approval_display(text, force=True))
            with patch.dict(sys.modules, {"gateway.run": redactor}):
                controls.register(sid, route, generation, data, timeout_s=data["timeout_s"])
            event_sid, event_type, prompt = events.get_nowait()
            assert (event_sid, event_type) == (sid, "approval.request")
            request_id = prompt["prompt_id"]
            assert call.open_demo(request_id, approve_once, prompt["timeout_s"])
            phrase = private_display[-1]
            assert phone_transcript == [PENDING_NOTICE] and phrase not in phone_transcript[0]
            print(phone_transcript[0])
            assert call.bind_capture(request_id, object()) is None
            answer = call.bind_capture(request_id, owner_mic)
            assert answer is not None and not answer("wrong words") and answer(phrase)
            assert not answer(phrase) and private_display[-1] is None
            result = await asyncio.wait_for(demo, 3)
            assert result["resolved"] and result["choice"] == "once"
            _, settled_type, settled = events.get_nowait()
            assert settled_type == "approval.settled" and settled["prompt_id"] == request_id
            assert not older.done(), "the older, unrelated approval must still be waiting"
            from tools.approval import resolve_gateway_approval
            assert resolve_gateway_approval(route, "deny", request_id=control["request_id"]) == 1
            assert (await asyncio.wait_for(older, 3))["choice"] == "deny"
        finally:
            call.close()
    print("Measured: Hermes resolved the exact demo request once. The older request stayed pending.")


if __name__ == "__main__":
    agent_path = Path(os.environ.get("HERMES_AGENT_SOURCE", Path.home() / ".hermes/hermes-agent")).resolve()
    sys.path.insert(0, str(agent_path))
    from pm.environments import selected_venv, site_packages, venv_python_version

    with patch.dict(os.environ, {"HERMES_HOME": str(agent_path.parent)}):
        hermes_venv = selected_venv(agent_path)
    if venv_python_version(hermes_venv) != sys.version_info[:2]:
        raise SystemExit("The Hermes and adapter virtual environments must use the same Python major/minor.")
    sys.path.append(str(site_packages(hermes_venv)))

    scratch = Path(os.environ.get("HERMES_HOME", agent_path.parent)) / "cache/scratch"
    with TemporaryDirectory(prefix="z0live-call-approvals-", dir=scratch) as home, patch.dict(os.environ, {
        "HERMES_HOME": home,
        "HERMES_MOBILE_VOICE_TRACE_DIR": home,
        "HERMES_VOICE_ENGINE": "gpt-live",
    }):
        asyncio.run(probe())

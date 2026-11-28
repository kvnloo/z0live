"""Approval notices have no microphone authorization path."""
import json
import os
import unittest
from unittest import mock

import voice_approvals as va


class NotificationTests(unittest.TestCase):
    def test_requires_explicit_opt_in(self):
        for config, expected in (({}, False), ({"Z0LIVE_VOICE_APPROVALS": "off"}, False),
                                 ({"Z0LIVE_VOICE_APPROVALS": "no"}, False),
                                 ({"Z0LIVE_VOICE_APPROVALS": "1"}, True)):
            with mock.patch.dict(os.environ, config, clear=True):
                self.assertEqual(va.enabled(), expected)

    def test_run_observes_approval_without_posting_an_answer(self):
        client = mock.Mock(spec=va.RunsClient)
        client.start.return_value = "run-1"
        request = {"event": "approval.request", "request_id": "request-1",
                   "command": "touch /tmp/example"}
        settled = {"event": "approval.responded", "request_id": "request-1", "choice": "deny"}
        client.events.return_value = iter([request, settled, {"event": "run.completed", "output": "Finished"}])
        notify = mock.Mock()

        self.assertEqual(va.run(client, "example", "session", "", notify)[:3], ("run-1", "Finished", ""))
        self.assertEqual(notify.call_args_list, [mock.call("run-1", request), mock.call("run-1", settled),
                                                mock.call("run-1", {"event": "approval.cleared"})])
        self.assertEqual([c[0] for c in client.method_calls], ["start", "events"])


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_notice_replay_and_guest_do_not_authorize(self):
        import mobile_live as live

        with mock.patch.dict(os.environ, {"HERMES_MOBILE_VOICE_TRACE": "0"}):
            call = live.Call(live.Manager(None), profile="tek")
        self.addAsyncCleanup(call.close, "test")
        call.speak_for = mock.Mock()
        call.trace = mock.Mock(wraps=call.trace)
        request = {"event": "approval.request", "request_id": "request-1", "command": "touch /tmp/example"}

        with mock.patch.object(live, "VOICE_APPROVALS", False):
            call.approval_event("delegation-1", "example", "run-1", request)
            call.speak_for.assert_not_called()

        with mock.patch.object(live, "VOICE_APPROVALS", True):
            for _ in range(2):
                call.approval_event("delegation-1", "example", "run-1", request)
            call.speak_for.assert_called_once()
            self.assertIn("authenticated Hermes approval interface", call.speak_for.call_args.args[1])
            self.assertNotIn("touch", repr(call.trace.call_args_list))
            with mock.patch.object(va.RunsClient, "_json") as rpc:
                for _ in range(2):
                    call.message(json.dumps({"type": "turn.done", "turn": {
                        "role": "user", "transcript": "Blue harbor lantern"}}), True)
                rpc.assert_not_called()
            self.assertEqual(set(call.approvals), {("example", "run-1", "request-1")})
            call.profile = "guest"
            call.approval_event("delegation-2", "example", "run-2", request)
            self.assertEqual(call.speak_for.call_count, 1)

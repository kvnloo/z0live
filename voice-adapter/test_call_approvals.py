import asyncio
import unittest

from call_approvals import CallApprovals


class CallApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner_mic = object()
        self.notices = []
        self.private = []
        self.resolved = []
        self.call = CallApprovals(owner_mic=self.owner_mic, send_notice=self.notices.append,
                                  show_private=self.private.append)
        self.addCleanup(self.call.close)

    def approve(self, request_id):
        self.resolved.append(request_id)
        return True

    def open_prompt(self, request_id="first", timeout_s: float = 5):
        self.assertTrue(self.call.open_demo(request_id, self.approve, timeout_s))
        answer = self.call.bind_capture(request_id, self.owner_mic)
        phrase = self.private[-1]
        assert answer is not None and phrase is not None
        return answer, phrase

    async def test_only_owner_can_approve_exact_request_once(self):
        self.assertIsNone(self.call.bind_capture("first", self.owner_mic))
        answer, phrase = self.open_prompt()
        self.assertIn("Approval pending", self.notices[-1])
        self.assertNotIn(phrase, self.notices[-1])
        self.assertEqual(len(phrase.split()), 6)
        self.assertIsNone(self.call.bind_capture("first", object()))
        self.assertIsNone(self.call.bind_capture("second", self.owner_mic))
        self.assertFalse(self.call.open_demo("second", self.approve, 5))
        self.assertFalse(answer("please " + phrase))
        self.assertTrue(answer("  " + phrase.upper() + "  "))
        self.assertFalse(answer(phrase))
        self.assertEqual(self.resolved, ["first"])
        self.assertIsNone(self.private[-1])

    async def test_stale_capture_cannot_approve_successor(self):
        stale, _ = self.open_prompt()
        self.call.settle("first")
        current, phrase = self.open_prompt("second")
        self.call.settle("first")
        self.assertFalse(stale(phrase))
        self.assertTrue(current(phrase))
        self.assertEqual(self.resolved, ["second"])

    async def test_expiry_withdraws_challenge(self):
        answer, phrase = self.open_prompt(timeout_s=0.01)
        await asyncio.sleep(0.03)
        self.assertIsNone(self.private[-1])
        self.assertFalse(answer(phrase))
        self.assertEqual(self.resolved, [])

    async def test_three_misses_consumes_prompt_without_approving(self):
        answer, phrase = self.open_prompt()
        for wrong in ("no", "猫", "x" * 129):
            self.assertFalse(answer(wrong))
        self.assertIsNone(self.private[-1])
        self.assertFalse(answer(phrase))
        self.assertEqual(self.resolved, [])

    async def test_close_rejects_pending_and_future_captures(self):
        answer, phrase = self.open_prompt()
        self.call.close()
        self.assertFalse(answer(phrase))
        self.assertFalse(self.call.open_demo("next", self.approve, 5))
        self.assertIsNone(self.private[-1])
        self.assertEqual(self.resolved, [])

    async def test_another_surface_winning_is_not_voice_success(self):
        self.assertTrue(self.call.open_demo("first", lambda request_id: False, 5))
        answer = self.call.bind_capture("first", self.owner_mic)
        phrase = self.private[-1]
        assert answer is not None and phrase is not None
        self.assertFalse(answer(phrase))
        self.assertIsNone(self.private[-1])

"""Negative controls and transport seams; stdlib only; no live requests."""

import json
from pathlib import Path
import unittest

import example
import validate_examples as check


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = json.loads(check.SAMPLE.read_text())
        cls.api = (check.ROOT / "docs/API.md").read_text()

    def test_fixture_matches_actual_api(self):
        self.assertEqual(check.validate(self.source, self.api), [])

    def test_old_wrapped_events_rejected(self):
        self.assertFalse(check.events({"events": []}))
        self.assertFalse(check.events([]))
        self.assertFalse(check.events([{"type": "missing"}]))

    def test_bare_inbox_rejected(self):
        self.assertFalse(check.inbox([]))
        self.assertFalse(check.inbox({"items": [], "after": 0}))
        self.assertFalse(check.inbox({"messages": [], "after": 0, "has_more": "false"}))

    def test_duplicate_requests_rejected(self):
        values = json.loads(json.dumps(self.source))
        values["requests"].append(values["requests"][0])
        self.assertTrue(any("duplicate" in err for err in check.validate(values, self.api)))

    def test_mutation_never_silently_retries(self):
        fake = example.FakeTransport({"/v1/tools/tell_hermes": (503, {"error": "unavailable"})})
        with self.assertRaises(ConnectionError):
            example.dispatch(
                fake, "POST", "/v1/tools/tell_hermes",
                {"session_id": "synthetic_session",
                 "arguments": {"text": "synthetic", "kind": "request"}},
                lambda: "injected-synthetic-bearer",
            )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["headers"]["Authorization"],
                         "Bearer injected-synthetic-bearer")
        self.assertNotIn("injected-synthetic-bearer", fake.calls[0]["url"])
        self.assertFalse(fake.calls[0]["allow_redirects"])

    def test_mock_queued_is_not_verified(self):
        fake = example.FakeTransport({
            "/v1/tools/tell_hermes": (200, {"ok": True, "status": "queued", "id": 1})
        })
        result = example.dispatch(fake, "POST", "/v1/tools/tell_hermes",
                                  {"session_id": "synthetic_session", "arguments": {}},
                                  lambda: "synthetic")
        self.assertEqual(result["status"], "queued")
        self.assertNotIn("verified", result)
        self.assertNotIn("completed", result)


if __name__ == "__main__":
    unittest.main()

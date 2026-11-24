"""Keep the catalog's stack access disclosures visible in both entry points."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CatalogDisclosuresTests(unittest.TestCase):
    def test_readme_and_operations_disclose_stack_access(self):
        heading = "## What this stack does beyond the plugin\n"
        for name in ("README.md", "docs/OPERATIONS.md"):
            with self.subTest(doc=name):
                doc = (ROOT / name).read_text()
                self.assertIn(heading, doc)
                body = doc.split(heading, 1)[1].split("\n## ", 1)[0]
                for term in ("~/.codex/auth.json", "chatgpt.com", "Codex Desktop",
                             "every local Hermes profile", "state.db", "read-only",
                             "desktop/hermes_relay.py", "gateway", "session.steer",
                             "prompt.submit", "Passthru", "adb", "calendar", "notifications",
                             "headless Chromium", "relays", "private-network-access checks", "CSP",
                             "Z0LIVE_PASSTHRU=off"):
                    self.assertIn(term, body)

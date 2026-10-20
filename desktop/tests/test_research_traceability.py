"""Offline completeness and research traceability contract for z0live (#32)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "docs" / "conformance" / "research-traceability.v1.json"
DOC = ROOT / "docs" / "RESEARCH_TRACEABILITY.md"


class ResearchTraceabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.doc = DOC.read_text(encoding="utf-8")

    def test_all_audited_records_covered_exactly_once(self) -> None:
        items = self.data["records"]
        self.assertEqual(set(items), {str(n) for n in range(1, 33)})
        self.assertEqual(
            sum(item["kind"] == "issue" for item in items.values()), 20
        )
        self.assertEqual(
            sum(item["kind"] == "pr" for item in items.values()), 12
        )
        self.assertEqual(self.data["audit"]["issues"], 20)
        self.assertEqual(self.data["audit"]["pull_requests"], 12)
        for number, item in items.items():
            suffix = "pull" if item["kind"] == "pr" else "issues"
            self.assertEqual(
                item["github"], f"https://github.com/kvnloo/z0live/{suffix}/{number}"
            )
            self.assertTrue(item["area"].strip())
            self.assertIn(item["priority"], self.data["gates"])
            self.assertNotEqual(item["status"], "merged")

    def test_research_topics_have_bijective_use_and_document_coverage(self) -> None:
        topics = set(self.data["research_topics"])
        self.assertEqual(topics, {f"R{n:02d}" for n in range(1, 16)})
        cited = set()
        for item in self.data["records"].values():
            self.assertTrue(item["research"])
            cited.update(item["research"])
        self.assertEqual(cited, topics)
        for topic in topics:
            self.assertIn(topic, self.doc)

    def test_archived_issues_are_historical_not_done(self) -> None:
        for n in (2, 3, 4, 5):
            self.assertEqual(self.data["records"][str(n)]["status"], "closed-not-planned")
        self.assertEqual(self.data["records"]["6"]["status"], "open-unmerged")
        self.assertEqual(self.data["records"]["7"]["status"], "open-unmerged")
        for n in (21, 22, 23, 24, 25, 26, 27, 28, 30, 31):
            self.assertEqual(self.data["records"][str(n)]["status"], "open-unmerged")

    def test_critical_quality_gates_present(self) -> None:
        for token in (
            "Braid", "StatePacketDelta", "SILENT", "WHEN_IDLE", "INTERRUPT",
            "first *audible*", "actual speaker silence", "voice_decision.v1",
            "zero side effects", "OMP", "replay-only", "no phone ADB",
        ):
            self.assertIn(token.casefold(), self.doc.casefold())
        self.assertEqual(self.data["records"]["32"]["priority"], "P0")
        self.assertEqual(self.data["records"]["29"]["priority"], "P4")
        self.assertIn("snapshot_date", self.data)
        self.assertIn("source_document", self.data["audit"])

    def test_every_record_has_a_human_readable_link(self) -> None:
        for item in self.data["records"].values():
            self.assertIn(item["github"], self.doc)


if __name__ == "__main__":
    unittest.main()

"""Static provenance/readiness constraints for portable vs desktop coexistence.

No network, CI, hardware or actor/provider runs are executed.
"""

import copy
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "docs/conformance/protocol-gap-ledger.json"
STATUSES = {
    "implemented_on_main", "unmerged_pr", "fixture_only",
    "blocked_hardware", "absent", "deferred_owner_priority",
}
EVIDENCE = {"source", "pr_diff", "fixture", "proposal"}
ID = re.compile(r"^zl-[0-9]{3}$")
ALLOWED_RUN = {"NOT_RUN", "BLOCKED"}


def validate(doc):
    errors = []
    if not isinstance(doc, dict) or doc.get("ledger_version") != 1:
        return ["ledger_version must equal 1"]
    rows = doc.get("rows")
    if not isinstance(rows, list) or not rows:
        return ["rows must be nonempty"]
    seen = set()
    for index, row in enumerate(rows):
        label = "rows[" + str(index) + "]"
        if not isinstance(row, dict):
            errors.append(label + " must be object")
            continue
        for field in (
            "id", "domain", "feature", "source_ref", "implementation_path",
            "status", "evidence_class", "next_action", "owner",
        ):
            if not isinstance(row.get(field), str) or not row[field].strip():
                errors.append(label + " missing " + field)
        ident = row.get("id", "")
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            errors.append(label + " invalid id")
        elif ident in seen:
            errors.append(label + " duplicate " + ident)
        else:
            seen.add(ident)
        status = row.get("status")
        evidence = row.get("evidence_class")
        ref = row.get("source_ref", "")
        path = row.get("implementation_path", "")
        if status not in STATUSES:
            errors.append(label + " invalid status")
        if evidence not in EVIDENCE:
            errors.append(label + " invalid evidence class")
        if not isinstance(row.get("hardware_gate"), bool) or not isinstance(
            row.get("provider_gate"), bool
        ):
            errors.append(label + " gate flags must be boolean")
        if row.get("device_result") not in ALLOWED_RUN:
            errors.append(label + " cannot claim real phone pass in documentation ledger")
        if row.get("provider_result") not in ALLOWED_RUN:
            errors.append(label + " cannot claim provider pass in documentation ledger")
        if status == "implemented_on_main":
            if evidence != "source" or not ref.startswith("main:desktop/"):
                errors.append(label + " main requires actual desktop default source")
            if not path.startswith("desktop/"):
                errors.append(label + " main desktop path is required")
        elif status == "unmerged_pr":
            if evidence != "pr_diff" or not any(
                f"/pull/{number}/" in ref for number in (6, 7)
            ):
                errors.append(label + " open PR is not default implementation")
            if path.startswith("desktop/") or path == "NO_IMPLEMENTATION":
                errors.append(label + " wrong portable path")
        elif status == "fixture_only":
            if evidence != "fixture" or "/pull/6/" not in ref or not path.startswith("fixtures/"):
                errors.append(label + " fixture-only cannot imply runtime/default proof")
        elif status == "blocked_hardware":
            if evidence != "source" or row.get("hardware_gate") is not True:
                errors.append(label + " hardware block must retain device gate")
        elif status == "absent":
            if evidence != "source":
                errors.append(label + " absent capability must cite draft/source only")
        elif status == "deferred_owner_priority":
            if evidence != "proposal" or "owner" not in row.get("owner", ""):
                errors.append(label + " deferred roadmap requires owner priority")
        if path.startswith("/") or ".." in Path(path).parts:
            errors.append(label + " unsafe path")
    return errors


class ProtocolLedgerConformance(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(LEDGER.read_text(encoding="utf-8"))

    def test_every_row_source_status_owner_and_next_action(self):
        self.assertEqual(validate(self.doc), [])
        self.assertGreaterEqual(len(self.doc["rows"]), 18)
        self.assertEqual({r["status"] for r in self.doc["rows"]}, STATUSES)

    def test_unmerged_green_pr_cannot_be_called_main(self):
        modified = copy.deepcopy(self.doc)
        row = next(r for r in modified["rows"] if r["status"] == "unmerged_pr")
        row["status"] = "implemented_on_main"
        row["evidence_class"] = "pr_diff"  # Even with old green CI, not default source.
        self.assertTrue(any("main" in issue for issue in validate(modified)))

    def test_fixture_alone_cannot_claim_provider_or_hardware_pass(self):
        modified = copy.deepcopy(self.doc)
        row = next(r for r in modified["rows"] if r["status"] == "fixture_only")
        row["provider_result"] = "PASS"
        self.assertTrue(any("provider pass" in issue for issue in validate(modified)))
        row["provider_result"] = "NOT_RUN"
        row["device_result"] = "PASS"
        self.assertTrue(any("phone pass" in issue for issue in validate(modified)))

    def test_absent_or_deferred_not_silently_marked_merged(self):
        modified = copy.deepcopy(self.doc)
        row = next(r for r in modified["rows"] if r["status"] == "absent")
        row["status"] = "implemented_on_main"
        self.assertTrue(any("main" in issue for issue in validate(modified)))
        deferred = next(r for r in modified["rows"] if r["status"] == "deferred_owner_priority")
        deferred["evidence_class"] = "source"
        self.assertTrue(any("owner priority" in issue for issue in validate(modified)))

    def test_source_and_owner_mandatory(self):
        modified = copy.deepcopy(self.doc)
        modified["rows"][0]["next_action"] = ""
        self.assertTrue(any("next_action" in issue for issue in validate(modified)))
        modified["rows"][1]["id"] = modified["rows"][0]["id"]
        self.assertTrue(any("duplicate" in issue for issue in validate(modified)))


if __name__ == "__main__":
    unittest.main()

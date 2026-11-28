"""C1-ctx-compress-r5 gate tests (no model). Fixture = r4 run 45918447833 job JSONs, scoring fields only."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bench"))
import ctx_compress_r2_verdict as v  # noqa: E402
import ctx_compress_r3_gates as g3  # noqa: E402
import ctx_compress_r5_gates as g5  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ctx_compress_r4_jobs.json"


def r4_jobs():
    return json.loads(FIXTURE.read_text())["jobs"]


def rows(jobs, label, planted):
    return [r for j in jobs for b in j["blocks"] if b["label"] == label for r in b["rows"]
            if (r["current"] is not None) == planted]


class R4Data(unittest.TestCase):
    def test_r4_verdict_keep_r3_gates_revert(self):
        jobs = r4_jobs()
        verdict = v.verdict(jobs)
        self.assertEqual(verdict["outcome"], "KEEP")
        self.assertEqual(verdict["recall"], {"A1": 18, "B1": 18, "A2": 18, "B2": 18, "O1": 9})
        self.assertEqual(g3.gates(verdict, jobs)["outcome"], "REVERT")  # the r3 rule counted the unplanted SKIP

    def test_r4_passes_r5_gates(self):
        jobs = r4_jobs()
        out = g5.gates(v.verdict(jobs), jobs)
        self.assertEqual(out["outcome"], "KEEP", out["gate_failures"])
        self.assertEqual(out["gate_failures"], [])
        for lab in ("B1", "B2"):
            self.assertEqual(out["unplanted_counts"][lab]["SKIP"], 1)
            self.assertEqual(out["unplanted_counts"][lab]["ABSTAIN"], out["unplanted_counts"]["A1"]["ABSTAIN"])

    def test_block_order_independent(self):
        jobs = r4_jobs()
        for j in jobs:
            j["blocks"] = [j["blocks"][i] for i in (1, 0, 3, 2, 4)]  # B1 A1 B2 A2 O1
        out = g5.gates(v.verdict(jobs), jobs)
        self.assertEqual(out["outcome"], "KEEP")


class Synthetic(unittest.TestCase):
    def test_b_skips_planted_fails(self):
        jobs = r4_jobs()
        r = rows(jobs, "B2", True)[0]
        r["reply"], r["result"] = "SKIP", "SKIP"
        out = g5.gates({"outcome": "KEEP"}, jobs)
        self.assertEqual(out["outcome"], "REVERT")
        self.assertEqual(out["gate_failures"], ["planted SKIP B2 1 > A max 0"])

    def test_b_supersedes_planted_fails(self):
        jobs = r4_jobs()
        rows(jobs, "B1", True)[0]["result"] = "SUPERSEDED"
        self.assertEqual(g5.gates({"outcome": "KEEP"}, jobs)["outcome"], "REVERT")

    def test_b_invents_on_unplanted_fails(self):
        jobs = r4_jobs()
        r = next(r for r in rows(jobs, "B1", False) if r["key"] == "ticket" and r["result"] != "SKIP")
        r["reply"], r["result"] = "Gallery-77", "INVENTED"
        out = g5.gates({"outcome": "KEEP"}, jobs)
        self.assertEqual(out["outcome"], "REVERT")
        self.assertIn("unplanted WRONG B1 4 > A max 3", out["gate_failures"])
        self.assertIn("unplanted INVENTED B1 1 > A max 0", out["gate_failures"])

    def test_b_wrong_off_format_on_unplanted_fails(self):
        jobs = r4_jobs()
        r = next(r for r in rows(jobs, "B2", False) if r["key"] == "ticket" and r["result"] != "SKIP")
        r["reply"], r["result"] = "Your usual studio code is Cedar-14.", "OFF_FORMAT"
        out = g5.gates({"outcome": "KEEP"}, jobs)
        self.assertEqual(out["gate_failures"], ["unplanted WRONG B2 4 > A max 3"])

    def test_skip_and_text_abstention_equivalent_on_unplanted(self):
        jobs = r4_jobs()
        for r in rows(jobs, "B1", False) + rows(jobs, "B2", False):
            if r["key"] == "ticket":
                r["reply"], r["result"] = "SKIP", "SKIP"
        self.assertEqual(g5.gates({"outcome": "KEEP"}, jobs)["outcome"], "KEEP")

    def test_null_and_revert_pass_through(self):
        jobs = r4_jobs()
        for o in ("NULL", "REVERT"):
            self.assertEqual(g5.gates({"outcome": o}, copy.deepcopy(jobs))["outcome"], o)

    def test_abstention_regex(self):
        for s in ("I do not have your gallery ticket code.", "I don't know that code.", "I'm not sure.",
                  "That is not in the context."):
            self.assertTrue(g5.ABSTAIN.search(s), s)
        for s in ("Your usual studio code is Cedar-14.", "Gallery-77", "I'm not able to say, it is Gallery-77"):
            self.assertFalse(g5.ABSTAIN.search(s), s)


if __name__ == "__main__":
    unittest.main()

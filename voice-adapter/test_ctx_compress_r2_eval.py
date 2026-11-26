"""C1-ctx-compress-r2 harness + verdict tests (no model)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bench"))
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r2_verdict as v  # noqa: E402
import tiers  # noqa: E402

SEP = "\n\nThis call so far:\n"


class Harness(unittest.TestCase):
    def test_only_chat_noise_shortened(self):
        for s, t in zip(context_eval.suite(), r2.suite()):
            self.assertEqual((s.name, s.stable, s.call, s.probes), (t.name, t.stable, t.call, t.probes))
            self.assertEqual([x for x in t.chat if x.update], [x for x in s.chat if x.update])  # same planted turns

    def test_full_brief_under_snapshot_cap_so_arm_a_holds_everything(self):
        for s in r2.suite():
            b = r2.chat_brief(s)
            self.assertTrue(2810 < len(b) <= 6000)
            self.assertIn(b, r2.context(s, "A"))
            for p in s.probes:
                if p.current:
                    self.assertIn(p.current, r2.context(s, "A"))

    def test_tail_cut_drops_every_brief_region_fact(self):
        for s in r2.suite():
            tail = r2.chat_brief(s)[-2810:]
            for p in s.probes:
                if p.key in v.BRIEF_KEYS:
                    self.assertNotIn(p.current, tail)
            self.assertEqual(r2.context(s, "O"), tiers.snapshot(tail, context_eval.exchange(s.call)))

    def test_only_the_brief_differs_between_arms(self):
        for s in r2.suite():
            calls = {r2.context(s, a).split(SEP)[1] for a in "ABO"}
            self.assertEqual(len(calls), 1)
            for a in "BO":
                self.assertLessEqual(len(r2.COMPRESS[a](r2.chat_brief(s))), 2810)

    def test_arm_b_is_r2_speaker_path(self):
        for s in r2.suite():
            self.assertTrue(r2.COMPRESS["B"](r2.chat_brief(s)).startswith(s.stable + "\n"))

    def test_probe_halves_partition(self):
        full = r2.suite()
        lo, hi = r2.select(full, "0,1,2,3"), r2.select(full, "4,5,6,7")
        for s, a, b in zip(full, lo, hi):
            self.assertEqual(a.probes + b.probes, s.probes)


def job(scen, half, rec):
    s = [x for x in r2.suite() if x.name == scen][0]
    probes = s.probes[:4] if half == "lo" else s.probes[4:]
    blocks = []
    for lab in v.LABELS:
        rows = [{"scenario": scen, "key": p.key, "current": p.current,
                 "result": "RECALL" if p.current and rec(lab, p.key) else "SKIP", "first_sentence_s": 1.0}
                for p in probes]
        blocks.append({"label": lab, "rows": rows})
    return {"scenarios": [scen], "complete": True, "blocks": blocks}


def jobs(rec):
    return [job(s, h, rec) for s in v.EXPECTED for h in ("lo", "hi")]


class Verdict(unittest.TestCase):
    def test_keep_when_b_matches_a(self):
        out = v.verdict(jobs(lambda lab, k: lab != "O1" or k not in v.BRIEF_KEYS))
        self.assertEqual(out["outcome"], "KEEP")
        self.assertEqual(out["recall"]["A1"], 18)
        self.assertEqual(out["recall"]["O1"], 9)
        self.assertIs(out["discriminates_O1_lt_A"], True)

    def test_revert_when_b_loses_vendor(self):
        out = v.verdict(jobs(lambda lab, k: not (lab[0] in "BO" and k == "vendor")))
        self.assertEqual(out["outcome"], "REVERT")
        self.assertEqual(out["recall"]["B1"], 15)
        self.assertEqual(out["brief_region_recall"]["B1"], 6)

    def test_o1_never_changes_outcome(self):
        a = v.verdict(jobs(lambda lab, k: True))
        b = v.verdict(jobs(lambda lab, k: lab != "O1"))
        self.assertEqual(a["outcome"], b["outcome"])
        self.assertIs(a["discriminates_O1_lt_A"], False)

    def test_null_on_missing_job_or_error(self):
        js = jobs(lambda lab, k: True)
        self.assertEqual(v.verdict(js[:-1])["outcome"], "NULL")
        js[0]["blocks"][1]["rows"][0]["result"] = "ERROR"
        self.assertEqual(v.verdict(js)["outcome"], "NULL")
        js = jobs(lambda lab, k: True)
        js[2]["complete"] = False
        self.assertEqual(v.verdict(js)["outcome"], "NULL")


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bench"))

from transport_bench import CONDITIONS, FREEZE_AT_MS, FREEZE_MS, SHAPE_HEAD, shape_schedule  # noqa: E402


class ShaperScheduleTest(unittest.TestCase):
    def test_same_seed_same_schedule_every_arm(self):
        for cond in CONDITIONS:
            self.assertEqual(shape_schedule(cond, 4100, 3000), shape_schedule(cond, 4100, 3000))
        self.assertNotEqual(shape_schedule("jitter25", 4100, 3000), shape_schedule("jitter25", 4101, 3000))

    def test_clean_is_zero_delay_no_loss(self):
        self.assertEqual(set(shape_schedule("clean", 1, 2000)), {0.0})

    def test_head_unshaped_and_loss_rate(self):
        s = shape_schedule("loss1_jitter5", 4100, 50 * 900)
        self.assertEqual(s[:SHAPE_HEAD], [0.0] * SHAPE_HEAD)
        lost = sum(d is None for d in s) / (len(s) - SHAPE_HEAD)
        self.assertTrue(0.007 < lost < 0.013, lost)
        self.assertTrue(all(d is None or d >= 0 for d in s))

    def test_freeze_releases_as_one_burst(self):
        s = shape_schedule("freeze300_loss1", 4100, 1000)
        end = FREEZE_AT_MS + FREEZE_MS
        frozen = [i for i, d in enumerate(s) if d is not None and d > 0]
        self.assertTrue(frozen)
        self.assertTrue(all(20 * i + s[i] == end for i in frozen))
        self.assertEqual(frozen[0], FREEZE_AT_MS // 20)

    def test_reorder_delays_3pct_by_65ms_no_loss(self):
        s = shape_schedule("reorder", 4100, 50 * 900)
        self.assertEqual(s[:SHAPE_HEAD], [0.0] * SHAPE_HEAD)
        self.assertEqual(set(s), {0.0, 65.0})
        late = sum(d == 65.0 for d in s) / (len(s) - SHAPE_HEAD)
        self.assertTrue(0.025 < late < 0.035, late)
        self.assertEqual(s, shape_schedule("reorder", 4100, 50 * 900))
        self.assertNotEqual(s, shape_schedule("reorder", 4101, 50 * 900))

    def test_earlier_conditions_schedule_unchanged_by_v4(self):
        # sha256 prefixes of shape_schedule(c, 4100, 3000) at transport_bench/3 (ec752fb).
        want = {"clean": "86c5820fa5dfba08", "jitter5": "e7901afd44faf6fc", "jitter25": "0f3d05bcdb4aadbf",
                "loss1_jitter5": "d097845e658c8de6", "freeze300_loss1": "61b1bbfd93f5a5fc"}
        for c, h in want.items():
            got = hashlib.sha256(json.dumps(shape_schedule(c, 4100, 3000)).encode()).hexdigest()[:16]
            self.assertEqual(got, h, c)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

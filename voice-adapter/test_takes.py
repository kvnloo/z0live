"""Every recorded film take (fixtures/takes, cut by bench/take_fixture.py with its words scrambled) replays through
the real adapter on each change and is judged by film call 1's five checks on what the phone got (replay.checks).
A new take is one more fixture; a check a take is known to fail only because of the recorded voice's own words is
listed in RECORDED, and must keep failing until the fixture is re-cut (a replay speaks the recorded words)."""
import asyncio
import json
import os
from pathlib import Path
import unittest

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config
import replay  # noqa: E402

TAKES = sorted(Path(__file__).with_name("fixtures").glob("takes/take*.jsonl"), key=lambda p: int(p.stem[4:]))
# Takes 7, 9 and 14: the voice's own hand-off line promised work ("I'll handle that and keep receipts", "I'm taking
# that on end-to-end", "I'll go step by step and show receipts"). The prompts are the guard; a replay cannot unsay it.
RECORDED = {"promised": {"take7", "take9", "take14"}}


def checked(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    r = replay.Replay(rows)
    asyncio.run(r.run())
    return replay.checks(r)


class RecordedTakes(unittest.TestCase):
    def test_the_takes_are_here(self):
        self.assertEqual([p.stem for p in TAKES][:8], [f"take{n}" for n in range(7, 15)])

    def test_each_recorded_take_passes_the_five_checks_on_the_phone(self):
        for path in TAKES:
            result = checked(path)
            for check, failures in result.items():
                with self.subTest(take=path.stem, check=check):
                    if path.stem in RECORDED.get(check, ()):
                        self.assertTrue(failures, "now passes: drop it from RECORDED")
                    else:
                        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()

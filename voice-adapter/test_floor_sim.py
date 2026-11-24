"""The floor simulator (floor_sim.py) on every change: SEEDS simulated film calls through the real adapter, each judged
by film call 1's five checks on what the phone got. A failure names the seed; `python floor_sim.py 1 SEED` replays it."""
import os
import unittest

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config
import floor_sim  # noqa: E402

SEEDS = int(os.environ.get("Z0LIVE_SIM_SEEDS", "24"))


class FloorSimulator(unittest.TestCase):
    def test_every_seeded_call_passes_the_five_checks(self):
        failed = {}
        for seed in range(SEEDS):
            broken = {check: found for check, found in floor_sim.run(seed).items() if found}
            if broken:
                failed[seed] = broken
        self.assertEqual(failed, {}, f"{len(failed)} of {SEEDS} seeds fail")

    def test_the_simulated_call_has_the_takes_shape(self):
        # The sim must exercise what broke takes 8-14: a name variant, a voice drafting over people talk, a provider
        # cut, an unnamed follow-up and Hermes slower than the next line.
        sim = floor_sim.Sim(3)
        names = {w.strip(",.") for r in sim.rows if (r.get("rx") or {}).get("type") == "turn.done"
                 for w in (r["rx"]["turn"].get("transcript") or "").split()}
        self.assertTrue(names & set(floor_sim.TEK))
        self.assertTrue(any(r.get("say") for r in sim.rows), "self-started drafts")
        self.assertEqual([r["to"] for r in sim.rows if "line" in r],
                         ["people", "people", "hermes", "hermes", "tek", "people"])


if __name__ == "__main__":
    unittest.main()

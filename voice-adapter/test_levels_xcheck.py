"""bench/levels_xcheck.py: levels_replay onsets vs bargein_replay sim truth (harness cross-check)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

original_path = sys.path[:]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
try:
    import levels_xcheck as x
finally:
    sys.path[:] = original_path


def first_trial(seed=39):
    cfg = x.sim_cfg(seed, 10)
    trial = next(iter(x.br.stimuli(1, seed, cfg.echo_model, cfg.bot_render, cfg.caller_level_db_range,
                                   cfg.erl_db_range, cfg.clip_set)))
    return cfg, trial


class SimPcm(unittest.TestCase):
    def test_sim_config_is_the_preregistered_acoustics(self):
        cfg = x.sim_cfg(39, 10)
        self.assertEqual((cfg.echo_model, cfg.echo_continuous, cfg.bot_render, cfg.clip_set),
                         ("residual", True, "recorded", "single"))

    def test_per_direction_pcm_from_the_sim(self):
        cfg, trial = first_trial()
        row, up, down = x.sim_trial(trial, cfg, "none")
        self.assertNotIn("adapter_flush_ms", row["trace"])            # arm none never flushes
        self.assertEqual(len(up), x.br.BOT_MS // x.br.CAPTURE_MS)
        self.assertTrue(all(p.size == x.br.CAPTURE_MS * x.br.SAMPLES_MS for _, p in up))
        self.assertEqual(up[0][0], x.br.CAPTURE_MS + trial["up"])     # capture end + uplink leg
        self.assertEqual(len(down), x.br.BOT_MS // x.br.FRAME_MS)
        self.assertEqual(down[0][0], cfg.audio_transit_ms + cfg.depth * x.br.FRAME_MS)
        bot = np.frombuffer(trial["bot"].tobytes(), dtype=np.int16)
        self.assertTrue(np.array_equal(down[5][1], bot[5 * 320:6 * 320]))
        # Continuous echo: the uplink carries energy while the bot plays, before the caller speaks.
        pre = np.concatenate([p for t, p in up if 300 <= t < trial["onset"]]).astype(np.float64)
        self.assertGreater(np.sqrt(np.mean(pre ** 2)), 100)

    def test_arm_a_downlink_goes_silent_after_its_flush(self):
        cfg, trial = first_trial()
        row, _, down = x.sim_trial(trial, cfg, "A")
        flush = row["trace"]["adapter_flush_ms"]
        self.assertTrue(all(not f.any() for t, f in down if t > flush))


class LevelsTrace(unittest.TestCase):
    def test_jsonl_comes_from_the_adapter_level_emitter(self):
        cfg, trial = first_trial()
        _, up, down = x.sim_trial(trial, cfg, "none")
        with tempfile.TemporaryDirectory() as d:
            lines = x.levels_jsonl(up, down, d)
        every = [json.loads(s) for s in lines]
        self.assertTrue({e["ev"] for e in every} <= {"audio.levels", "audio.on", "audio.off"})  # Level's own events
        events = [e for e in every if e["ev"] == "audio.levels"]
        self.assertTrue(events)
        by_dir = {e["dir"] for e in events}
        self.assertEqual(by_dir, {"input", "output"})
        first_up = next(e for e in events if e["dir"] == "input")
        self.assertEqual(first_up["n"], 160)
        self.assertEqual(first_up["t"][0], up[0][0])
        peak = int(np.abs(up[0][1].astype(np.int32)).max())
        self.assertEqual([first_up["rms"][0], first_up["peak"][0]], list(x.live.frame_dbfs(up[0][1], peak)))
        self.assertEqual(next(e for e in events if e["dir"] == "output")["n"], 320)
        self.assertNotIn("Z0LIVE_TRACE_LEVELS", os.environ)            # env patch did not leak


class Scoring(unittest.TestCase):
    def test_match_window(self):
        on = [{"t_ms": 850}, {"t_ms": 1010}, {"t_ms": 1500}]
        hit, false = x.match(on, 1000, "valid")
        self.assertIs(hit, on[1])
        self.assertEqual(len(false), 2)
        self.assertEqual(x.match(on, 1000, "echo"), (None, on))
        self.assertIsNone(x.match([{"t_ms": 1401}], 1000, "valid")[0])

    def test_verdict_thresholds(self):
        def rows(n_valid, hit, err, false):
            out = [{"seed": 1, "id": i, "kind": "valid", "detected": i < hit, "err_ms": err if i < hit else None,
                    "false_onsets": 0, "down_active_at_truth": True} for i in range(n_valid)]
            out[0]["false_onsets"] = false
            return out
        self.assertEqual(x.summarise(rows(10, 9, 40, 1))["verdict"], "AGREE")
        self.assertEqual(x.summarise(rows(10, 8, 40, 0))["verdict"], "DISAGREE")   # detection 0.8
        self.assertEqual(x.summarise(rows(10, 10, 61, 0))["verdict"], "DISAGREE")  # median 61 ms
        self.assertEqual(x.summarise(rows(10, 10, 20, 2))["verdict"], "DISAGREE")  # 0.2 false / trial

    def test_tiny_run_schema_and_determinism(self):
        a = x.run(seeds=(39,), trials=2, arms=("none",))
        b = x.run(seeds=(39,), trials=2, arms=("none",))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(a["schema"], "levels_xcheck/1")
        self.assertEqual(a["summary"]["n_trials"], 2)
        self.assertEqual(len(a["arms"]["none"]["trials"]), 2)
        self.assertEqual(a["scorer"]["params"]["floor_mode"], "p95")   # scorer default (A-levels-floor: REVERT)

    def test_floor_mode_reaches_the_scorer(self):
        a = x.run(seeds=(39,), trials=1, arms=("none",), floor_mode="p50")
        self.assertEqual(a["scorer"]["params"]["floor_mode"], "p50")
        self.assertEqual(a["sim"]["arms"], ["none"])


class Preregistered(unittest.TestCase):
    """The preregistered run (seeds 39, 40 x 30, arms none + A): prints the summary; asserts plumbing, not verdict."""
    def test_preregistered_run(self):
        res = x.run()
        s = res["summary"]
        self.assertEqual((s["n_trials"], s["n_valid"]), (60, 42))
        self.assertIn(s["verdict"], ("AGREE", "DISAGREE"))
        for t in res["arms"]["none"]["trials"]:
            self.assertEqual(t["integrity"]["input"]["non_monotonic_t"], 0)
            self.assertEqual(t["integrity"]["output"]["duplicate_t"], 0)
        brief = {arm: v["summary"] for arm, v in res["arms"].items()}
        sys.stderr.write("\nLEVELS_XCHECK_SUMMARY " + json.dumps(brief, sort_keys=True) + "\n")
        out = os.environ.get("LEVELS_XCHECK_OUT")
        if out:
            Path(out).write_text(json.dumps(res, sort_keys=True, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

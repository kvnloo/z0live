"""Offline metric checks; optional inference packages are deliberately absent."""
import importlib.util
import hashlib
import json
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("voice_bench", Path(__file__).parents[1] / "voice_bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkMetrics(unittest.TestCase):
    def test_word_errors(self):
        self.assertEqual(bench.word_errors("Wait, STOP!", "wait stop"), (0, 2))
        self.assertEqual(bench.word_errors("check z0live branch", "check branch now"), (2, 3))
        self.assertEqual(bench.word_errors("open Spotify", ""), (2, 2))

    def test_nearest_rank_tail(self):
        self.assertEqual(bench.percentile([10, 20, 30, None], .95), 30)
        self.assertIsNone(bench.percentile([], .5))

    def test_failure_denominator_and_unknowns(self):
        summary = bench.summarize([
            {"first_pcm_ms": 100, "wer_errors": 0, "wer_words": 2, "answer_keyword_pass": True},
            {"first_pcm_ms": None, "wer_errors": 2, "wer_words": 2}])
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["complete_n"], 1)
        self.assertEqual(summary["wer_pct"], 50)
        self.assertIsNone(summary["barge_stop_ms"])
        self.assertIsNone(summary["total_cost_usd_per_hour"])
        self.assertIsNone(summary["task_correctness"])
        self.assertEqual(summary["release_gate"], "PARTIAL")

    def test_prepare_emits_hashed_synthetic_manifest(self):
        with TemporaryDirectory() as directory:
            def synthesize(command, **kwargs):
                Path(command[-1]).write_bytes(kwargs["input"].encode())
            with patch.object(bench.subprocess, "run", side_effect=synthesize) as runner:
                bench.prepare(SimpleNamespace(audio=Path(directory), piper="fake-piper", voice="voice"))
            manifest = json.loads((Path(directory) / "manifest.json").read_text())
            self.assertEqual(runner.call_count, 8)
            self.assertEqual(len(manifest["cases"]), 8)
            for case in manifest["cases"]:
                self.assertEqual(case["sha256"], hashlib.sha256((Path(directory) / case["file"]).read_bytes()).hexdigest())

import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
import context_eval as context
import truth_eval as truth


class TruthReplayTests(unittest.TestCase):
    def test_scores_only_matching_spoken_results_with_a_final(self):
        def event(name, did='d1', **fields):
            return {'ev': name, 'did': did, **fields}

        fast = {'tier': 'fast', 'model': 'fast-model'}
        slow = {'tier': 'slow', 'model': 'slow-model'}
        rows = [event('delegation.start', request='Ask one'),
                event('delegation.start', 'd2', request='Ask two'),
                event('lanes.tier', 'd2', text='Different turn', s=0.5, **fast),
                event('lanes.spoken', 'd2', **fast),
                event('lanes.tier', text='Spoken answer', s=1.2, **fast),
                event('lanes.spoken', first=True, **fast),
                event('lanes.tier', text='Silent answer', s=4.5, **slow),
                event('delegation.done', answer='Hermes one'),
                event('lanes.spoken', 'd3', **fast)]
        with tempfile.TemporaryDirectory() as tmp:
            a, b = (Path(tmp) / name for name in ('a.jsonl', 'b.jsonl'))
            a.write_text('\n'.join(map(json.dumps, rows)) + '\n{')
            b.write_text(json.dumps(event('delegation.done', 'd2', answer='Different call')))
            samples, times, counts = truth.collect([a, b])
        self.assertEqual(samples, [truth.Sample('Ask one', 'Hermes one', 'Spoken answer', 'fast', 'fast-model', True)])
        self.assertEqual(times, {'fast': [0.5, 1.2], 'slow': [4.5]})
        self.assertEqual(counts['unpaired_spoken'], 2)
        self.assertEqual(counts['invalid_json'], 1)

    def test_uncertain_is_not_clean(self):
        summary = truth.summarize(['CLEAN', 'HALLUCINATION', 'UNCERTAIN', 'ERROR'])
        self.assertEqual(summary['hallucinations_per_answer']['rate'], 0.25)
        self.assertEqual(summary['hallucinations_per_determinate_answer']['rate'], 0.5)
        self.assertEqual(summary['unresolved'], 2)
        self.assertIsNone(truth.summarize([])['hallucinations_per_answer']['rate'])


class ContextTests(unittest.TestCase):
    def test_budget_never_splits_turns_or_leaves_stale_blackboard_values(self):
        scenario = context.suite()[0]
        rendered, tokens, recent, facts = context.budgeted(scenario, len, 410, 6)
        self.assertLessEqual(tokens, 410)
        self.assertLess(recent, 6)
        self.assertEqual(facts, 3)
        self.assertIn('Flight weekday: Saturday', rendered)
        self.assertNotIn('Thursday', rendered)
        self.assertNotIn('Monday', rendered)
        self.assertIn('Elm-24.', rendered)
        self.assertIn('I can remind you when you head over.', rendered)
        self.assertIn('Thursday', context.snapshot(scenario))
        # C56 wiring: the compressor keeps a reply-less user statement (Mint-57) as a fact; the old 6000-char tail cut dropped it
        self.assertIn('Mint-57', context.snapshot(scenario))
        with self.assertRaises(ValueError):
            context.budgeted(scenario, len, 2, 0)

    def test_wrong_stale_absent_and_malformed_have_separate_outcomes(self):
        probe = context.Probe('flight', 'Which day?', 'Thursday', ('Monday', 'Tuesday'))
        absent = context.Probe('ticket', 'Which ticket?', None)
        self.assertEqual(context.score(probe, 'thursday.'), 'RECALL')
        self.assertEqual(context.score(probe, 'Your flight is now on Thursday.'), 'RECALL')
        self.assertEqual(context.score(probe, 'Your flight is on Tuesday.'), 'SUPERSEDED')
        self.assertEqual(context.score(probe, 'Your flight is on Thursday. Your room is Birch-40.'), 'OFF_FORMAT')
        self.assertEqual(context.score(probe, 'Tuesday'), 'SUPERSEDED')
        self.assertEqual(context.score(probe, 'Saturday'), 'INVENTED')
        self.assertEqual(context.score(absent, 'Mint-90'), 'INVENTED')
        self.assertEqual(context.score(absent, 'SKIP'), 'SKIP')
        self.assertEqual(context.score(probe, 'Thursday or Friday'), 'OFF_FORMAT')
        self.assertEqual(context.score(probe, ''), 'ERROR')

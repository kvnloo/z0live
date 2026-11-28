"""Stdlib tests for truth_adj (no model, no network). Run from voice-adapter/bench: python -m unittest test_truth_adj"""
import hashlib
import json
from pathlib import Path
import re
import unittest

import truth_adj
import truth_eval

HERE = Path(__file__).resolve().parent


class FakeModel:
    """Answers from a lane_reply -> label table; records every call."""

    def __init__(self, table=None, default='CLEAN', fail=()):
        self.table, self.default, self.fail, self.calls = table or {}, default, set(fail), []
        self.info = {'id': 'fake'}

    def ask(self, system, user, max_tokens, *, literal=False):
        self.calls.append((system, json.loads(user), max_tokens, literal))
        reply = json.loads(user)['lane_reply']
        if reply in self.fail:
            raise RuntimeError('local reply did not finish normally')
        return self.table.get(reply, self.default), 1


def oracle(items):
    table = {item['lane_reply']: item['gold'] for item in items}
    table.update({reply: gold for _, _, reply, gold in truth_eval.CONTROLS})
    return table


class SetTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.sha = truth_adj.load_set(truth_adj.SET)
        self.items = self.doc['items']

    def test_schema_and_balance(self):
        self.assertEqual(self.doc['format'], 'truth_adj/1')
        self.assertTrue(55 <= len(self.items) <= 65)
        self.assertEqual(len({i['id'] for i in self.items}), len(self.items))
        for label in truth_eval.LABELS:
            n = sum(i['gold'] == label for i in self.items)
            self.assertTrue(18 <= n <= 22, (label, n))
        for item in self.items:
            self.assertEqual(set(item), {'id', 'split', 'category', 'question', 'hermes_reference', 'lane_reply', 'gold'})
            self.assertIn(item['gold'], truth_eval.LABELS)
            self.assertTrue(item['hermes_reference'] is None or isinstance(item['hermes_reference'], str))
        self.assertGreaterEqual(sum(i['category'] == 'hard' for i in self.items), 3)

    def test_splits_alternate(self):
        self.assertEqual([i['split'] for i in self.items],
                         ['dev' if n % 2 == 0 else 'holdout' for n in range(len(self.items))])

    def test_synthetic_only(self):
        for item in self.items:
            for field in ('question', 'hermes_reference', 'lane_reply'):
                text = item[field] or ''
                self.assertIsNone(re.search(r'(^|\s)[~/]\w|https?:|@\w', text), (item['id'], field))

    def test_sha_is_file_bytes(self):
        self.assertEqual(self.sha, hashlib.sha256(truth_adj.SET.read_bytes()).hexdigest())

    def test_controls_kept_separate(self):
        replies = {i['lane_reply'] for i in self.items}
        self.assertFalse(replies & {reply for _, _, reply, _ in truth_eval.CONTROLS})

    def test_validate_rejects_bad_gold(self):
        bad = json.loads(json.dumps(self.doc))
        bad['items'][0]['gold'] = 'MAYBE'
        with self.assertRaises(ValueError):
            truth_adj.validate(bad)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.items = truth_adj.load_set(truth_adj.SET)[0]['items']

    def test_uses_truth_eval_rubric_and_grammar(self):
        model = FakeModel(oracle(self.items))
        truth_adj.run(model, self.items[:2])
        self.assertTrue(model.calls)
        for system, data, _, literal in model.calls:
            self.assertEqual(system, truth_eval.RUBRIC)
            self.assertTrue(literal)
            self.assertEqual(set(data), {'question', 'hermes_reference', 'lane_reply'})

    def test_perfect_grader(self):
        out = truth_adj.run(FakeModel(oracle(self.items)), self.items)
        m = truth_adj.metrics(out['rows'])
        self.assertEqual(m['accuracy']['overall']['rate'], 1.0)
        self.assertEqual(m['accuracy']['dev']['rate'], 1.0)
        self.assertEqual(m['accuracy']['holdout']['rate'], 1.0)
        self.assertEqual(m['clean_false_flag']['rate'], 0.0)
        self.assertEqual(m['errors'], 0)
        self.assertEqual(out['controls_passed'], 5)
        for gold, row in m['confusion'].items():
            self.assertEqual(sum(v for k, v in row.items() if k != gold), 0)
        self.assertEqual([r['id'] for r in out['rows']], [i['id'] for i in self.items])

    def test_flag_everything(self):
        out = truth_adj.run(FakeModel(default='HALLUCINATION'), self.items)
        m = truth_adj.metrics(out['rows'])
        n_h = sum(i['gold'] == 'HALLUCINATION' for i in self.items)
        self.assertEqual(m['hallucination']['recall']['rate'], 1.0)
        self.assertAlmostEqual(m['hallucination']['precision']['rate'], n_h / len(self.items))
        self.assertEqual(m['clean_false_flag']['rate'], 1.0)
        self.assertEqual(m['recall']['UNCERTAIN']['rate'], 0.0)
        self.assertEqual(out['controls_passed'], 2)

    def test_errors_counted_and_in_confusion(self):
        first = self.items[0]
        out = truth_adj.run(FakeModel(oracle(self.items), fail={first['lane_reply']}), self.items)
        m = truth_adj.metrics(out['rows'])
        self.assertEqual(m['errors'], 1)
        self.assertEqual(out['rows'][0]['label'], 'ERROR')
        self.assertEqual(m['confusion'][first['gold']]['ERROR'], 1)
        self.assertEqual(m['accuracy']['overall']['n'], len(self.items) - 1)

    def test_off_label_is_error(self):
        out = truth_adj.run(FakeModel(default='MAYBE'), self.items[:3])
        self.assertEqual({r['label'] for r in out['rows']}, {'ERROR'})

    def test_flips(self):
        a = [{'id': 'x', 'label': 'CLEAN'}, {'id': 'y', 'label': 'HALLUCINATION'}]
        b = [{'id': 'y', 'label': 'CLEAN'}, {'id': 'x', 'label': 'CLEAN'}]
        self.assertEqual(truth_adj.flips(a, b), ['y'])
        with self.assertRaises(ValueError):
            truth_adj.flips(a, b[:1])


if __name__ == '__main__':
    unittest.main()

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


DEFAULT_RUBRIC_SHA256 = '2814a72b07c81a0f8bb1f388150352245bb74c7422dec7d4d1bb0fb63de1c8a8'  # truth_eval.RUBRIC at b107468
HOLDOUT2 = HERE / 'truth_adj_holdout2.json'


class RubricVariantTests(unittest.TestCase):
    def test_default_rubric_byte_identical(self):
        self.assertEqual(hashlib.sha256(truth_eval.RUBRIC.encode()).hexdigest(), DEFAULT_RUBRIC_SHA256)
        self.assertIs(truth_eval.RUBRIC_VARIANTS['default'], truth_eval.RUBRIC)

    def test_variants_add_exactly_one_clause(self):
        variants = truth_eval.RUBRIC_VARIANTS
        self.assertTrue(2 <= len(variants) <= 4)  # default + at most 3 wordings
        for name, clause in truth_eval.HANDOFF_CLAUSES.items():
            text = variants[name]
            self.assertNotEqual(text, truth_eval.RUBRIC)
            self.assertEqual(text.count(clause), 1)
            self.assertEqual(text.replace(' ' + clause, '', 1), truth_eval.RUBRIC)
            self.assertIn('Hermes or another agent', clause)
            self.assertTrue(text.endswith('Return only HALLUCINATION, CLEAN or UNCERTAIN.'))
            for _, _, reply, _ in truth_eval.CONTROLS:
                self.assertNotIn(reply, clause)  # no control text leaks into the clause
        self.assertEqual(set(variants), {'default', *truth_eval.HANDOFF_CLAUSES})

    def test_grade_default_and_variant(self):
        model = FakeModel()
        truth_eval.grade(model, 'q', None, 'r')
        name = sorted(truth_eval.HANDOFF_CLAUSES)[0]
        truth_eval.grade(model, 'q', None, 'r', rubric=truth_eval.RUBRIC_VARIANTS[name])
        self.assertEqual(model.calls[0][0], truth_eval.RUBRIC)
        self.assertEqual(model.calls[1][0], truth_eval.RUBRIC_VARIANTS[name])

    def test_run_passes_variant_to_items_and_controls(self):
        items = truth_adj.load_set(truth_adj.SET)[0]['items'][:3]
        name = sorted(truth_eval.HANDOFF_CLAUSES)[-1]
        model = FakeModel()
        out = truth_adj.run(model, items, rubric=truth_eval.RUBRIC_VARIANTS[name])
        self.assertEqual(len(model.calls), 3 + len(truth_eval.CONTROLS))
        self.assertEqual({c[0] for c in model.calls}, {truth_eval.RUBRIC_VARIANTS[name]})
        self.assertEqual(len(out['controls']), len(truth_eval.CONTROLS))


class Holdout2Tests(unittest.TestCase):
    def setUp(self):
        self.doc, self.sha = truth_adj.load_set(HOLDOUT2)
        self.items = self.doc['items']

    def test_schema_balance_and_coverage(self):
        self.assertEqual(self.doc['format'], 'truth_adj/2')
        self.assertTrue(30 <= len(self.items) <= 40)
        for label in truth_eval.LABELS:
            self.assertEqual(sum(i['gold'] == label for i in self.items), 12, label)
        self.assertEqual({i['split'] for i in self.items}, {'holdout2'})
        cat = lambda c, g: sum(i['category'] == c and i['gold'] == g for i in self.items)  # noqa: E731
        self.assertGreaterEqual(cat('handoff', 'CLEAN'), 6)
        self.assertGreaterEqual(cat('loophole', 'HALLUCINATION'), 4)
        self.assertGreaterEqual(cat('unresolved_null', 'UNCERTAIN'), 3)
        handoffs = ' '.join(i['lane_reply'] for i in self.items if i['category'] == 'handoff')
        for phrase in (' can ', ' will ', 'is going to', "I'll get Hermes to"):
            self.assertIn(phrase, handoffs)

    def test_disjoint_from_dev_and_controls(self):
        dev = truth_adj.load_set(truth_adj.SET)[0]['items']
        self.assertFalse({i['id'] for i in self.items} & {i['id'] for i in dev})
        replies = {i['lane_reply'] for i in self.items}
        self.assertFalse(replies & {i['lane_reply'] for i in dev})
        self.assertFalse(replies & {reply for _, _, reply, _ in truth_eval.CONTROLS})

    def test_synthetic_only(self):
        for item in self.items:
            for field in ('question', 'hermes_reference', 'lane_reply'):
                text = item[field] or ''
                self.assertIsNone(re.search(r'(^|\s)[~/]\w|https?:|@\w', text), (item['id'], field))

    def test_grade_sets_tags_rows(self):
        dev = truth_adj.load_set(truth_adj.SET)[0]
        out = truth_adj.grade_sets(FakeModel(), [('holdout2', self.doc), ('adj1', dev)], truth_eval.RUBRIC)
        self.assertEqual(len(out['rows']), len(self.items) + len(dev['items']))
        self.assertEqual({r['set'] for r in out['rows']}, {'holdout2', 'adj1'})
        self.assertEqual(len(out['controls']), len(truth_eval.CONTROLS))
        with self.assertRaises(ValueError):
            truth_adj.grade_sets(FakeModel(), [('a', dev), ('b', dev)], truth_eval.RUBRIC)


if __name__ == '__main__':
    unittest.main()

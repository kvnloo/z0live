"""Stdlib tests for truth_adj (no model, no network). Run from voice-adapter/bench: python -m unittest test_truth_adj"""
import hashlib
import inspect
import json
from pathlib import Path
import re
import unittest

import eval_local
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
        self.assertTrue(1 <= len(truth_eval.HANDOFF_CLAUSES) <= 3)  # at most 3 handoff wordings
        for name, clause in truth_eval.HANDOFF_CLAUSES.items():
            text = variants[name]
            self.assertNotEqual(text, truth_eval.RUBRIC)
            self.assertEqual(text.count(clause), 1)
            self.assertEqual(text.replace(' ' + clause, '', 1), truth_eval.RUBRIC)
            self.assertIn('Hermes or another agent', clause)
            self.assertTrue(text.endswith('Return only HALLUCINATION, CLEAN or UNCERTAIN.'))
            for _, _, reply, _ in truth_eval.CONTROLS:
                self.assertNotIn(reply, clause)  # no control text leaks into the clause
        self.assertEqual(set(variants), {'default', *truth_eval.HANDOFF_CLAUSES, *truth_eval.UNC_CLAUSES})

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


DEFAULT_GRADE_SOURCE_SHA256 = '243dfc799ebf7ffba436f2bbae02ce653201bc6814009a6e0acea19b7991dfd3'  # truth_eval.grade at 9c6c3ad
HOLDOUT3 = HERE / 'truth_adj_holdout3.json'


class FakeTwoStep:
    """Step 1 (YES/NO grammar) answers from a reply -> YES|NO table; step 2 answers from a reply -> label table."""

    def __init__(self, defer=None, labels=None, fail1=(), off1=()):
        self.defer, self.labels, self.fail1, self.off1, self.calls = defer or {}, labels or {}, set(fail1), set(off1), []
        self.info = {'id': 'fake'}

    def ask(self, system, user, max_tokens, *, literal=False, grammar=None):
        data = json.loads(user)
        self.calls.append((system, data, max_tokens, literal, grammar))
        reply = data['lane_reply']
        if grammar is not None:
            if reply in self.fail1:
                raise RuntimeError('local reply did not finish normally')
            return ('MAYBE' if reply in self.off1 else self.defer.get(reply, 'NO')), 1
        return self.labels.get(reply, 'HALLUCINATION'), 1


class TwoStepTests(unittest.TestCase):
    def test_default_grade_and_rubric_unchanged(self):
        self.assertEqual(hashlib.sha256(truth_eval.RUBRIC.encode()).hexdigest(), DEFAULT_RUBRIC_SHA256)
        self.assertEqual(hashlib.sha256(inspect.getsource(truth_eval.grade).encode()).hexdigest(),
                         DEFAULT_GRADE_SOURCE_SHA256)

    def test_deferral_prompts(self):
        prompts = truth_eval.DEFERRAL_PROMPTS
        self.assertTrue(1 <= len(prompts) <= 2)  # at most 2 wordings for DEV selection
        self.assertEqual(truth_eval.YES_NO_GRAMMAR, 'root ::= "YES" | "NO"')
        held = [i['lane_reply'] for i in truth_adj.load_set(HOLDOUT3)[0]['items']]
        for name, text in prompts.items():
            self.assertTrue(text.endswith('Return only YES or NO.'), name)
            for _, _, reply, _ in truth_eval.CONTROLS:
                self.assertNotIn(reply, text)
            for reply in held:
                self.assertNotIn(reply, text)

    def test_yes_is_clean_with_one_call(self):
        model = FakeTwoStep(defer={'Hermes will do it.': 'YES'})
        name = sorted(truth_eval.DEFERRAL_PROMPTS)[0]
        label, step1 = truth_eval.grade_two_step(model, 'q', None, 'Hermes will do it.', name)
        self.assertEqual((label, step1), ('CLEAN', 'YES'))
        self.assertEqual(len(model.calls), 1)
        system, data, _, _, grammar = model.calls[0]
        self.assertEqual(system, truth_eval.DEFERRAL_PROMPTS[name])
        self.assertEqual(grammar, truth_eval.YES_NO_GRAMMAR)
        self.assertEqual(data, {'lane_reply': 'Hermes will do it.'})

    def test_no_runs_unchanged_default_rubric(self):
        model = FakeTwoStep(labels={'It is 5.': 'UNCERTAIN'})
        name = sorted(truth_eval.DEFERRAL_PROMPTS)[-1]
        label, step1 = truth_eval.grade_two_step(model, 'q', 'ref', 'It is 5.', name)
        self.assertEqual((label, step1), ('UNCERTAIN', 'NO'))
        self.assertEqual(len(model.calls), 2)
        system, data, max_tokens, literal, grammar = model.calls[1]
        self.assertEqual(system, truth_eval.RUBRIC)
        self.assertTrue(literal)
        self.assertIsNone(grammar)
        self.assertEqual(max_tokens, 16)
        self.assertEqual(data, {'question': 'q', 'hermes_reference': 'ref', 'lane_reply': 'It is 5.'})

    def test_step1_failure_or_off_label_is_error_without_step2(self):
        name = sorted(truth_eval.DEFERRAL_PROMPTS)[0]
        for model in (FakeTwoStep(fail1={'r'}), FakeTwoStep(off1={'r'})):
            label, step1 = truth_eval.grade_two_step(model, 'q', None, 'r', name)
            self.assertEqual((label, step1), ('ERROR', 'ERROR'))
            self.assertEqual(len(model.calls), 1)

    def test_grade_sets_two_step_records_step1(self):
        items = truth_adj.load_set(truth_adj.SET)[0]['items'][:4]
        defer = {items[0]['lane_reply']: 'YES', 'Hermes can take that one.': 'YES'}
        name = sorted(truth_eval.DEFERRAL_PROMPTS)[0]
        model = FakeTwoStep(defer=defer)
        out = truth_adj.grade_sets(model, [('a', {'items': items})], mode='two_step', deferral=name)
        self.assertEqual([r['step1'] for r in out['rows']], ['YES', 'NO', 'NO', 'NO'])
        self.assertEqual(out['rows'][0]['label'], 'CLEAN')
        self.assertEqual(len(out['controls']), len(truth_eval.CONTROLS))
        self.assertIn('step1', out['controls'][3])
        self.assertEqual(out['controls'][3]['label'], 'CLEAN')
        # calls: 4 items + 5 controls step 1, then step 2 for every NO (3 items + 4 controls)
        self.assertEqual(len(model.calls), 9 + 7)

    def test_single_mode_rows_unchanged(self):
        items = truth_adj.load_set(truth_adj.SET)[0]['items'][:2]
        out = truth_adj.grade_sets(FakeModel(), [('a', {'items': items})])
        self.assertEqual(set(out['rows'][0]), {'set', 'id', 'split', 'category', 'gold', 'label'})
        self.assertEqual(set(out['controls'][0]), {'expected', 'label'})

    def test_cli_mode_flags(self):
        parser = truth_adj.build_parser()
        args = parser.parse_args([])
        self.assertEqual((args.mode, args.deferral_prompt), ('single', None))
        name = sorted(truth_eval.DEFERRAL_PROMPTS)[0]
        args = parser.parse_args(['--mode', 'two_step', '--deferral-prompt', name])
        self.assertEqual((args.mode, args.deferral_prompt), ('two_step', name))
        with self.assertRaises(SystemExit):
            parser.parse_args(['--mode', 'other'])

    def test_local_model_grammar_kwarg(self):
        model = eval_local.LocalModel.__new__(eval_local.LocalModel)
        model.model, bodies = 'm', []

        def request(path, body=None):
            bodies.append(body)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'YES'}}], 'usage': {'prompt_tokens': 3}}

        model.request = request
        model.ask('s', 'u', 4, grammar=truth_eval.YES_NO_GRAMMAR)
        model.ask('s', 'u', 16, literal=True)
        model.ask('s', 'u', 16)
        self.assertEqual(bodies[0]['grammar'], truth_eval.YES_NO_GRAMMAR)
        self.assertEqual(bodies[1]['grammar'], 'root ::= "HALLUCINATION" | "CLEAN" | "UNCERTAIN"')
        self.assertNotIn('grammar', bodies[2])


class Holdout3Tests(unittest.TestCase):
    def setUp(self):
        self.doc, self.sha = truth_adj.load_set(HOLDOUT3)
        self.items = self.doc['items']

    def test_schema_balance_and_coverage(self):
        self.assertEqual(self.doc['format'], 'truth_adj/2')
        self.assertGreaterEqual(len(self.items), 36)
        for label in truth_eval.LABELS:
            self.assertEqual(sum(i['gold'] == label for i in self.items), 12, label)
        self.assertEqual({i['split'] for i in self.items}, {'holdout3'})
        cat = lambda c, g: sum(i['category'] == c and i['gold'] == g for i in self.items)  # noqa: E731
        self.assertGreaterEqual(cat('loophole', 'HALLUCINATION'), 8)
        self.assertGreaterEqual(cat('handoff', 'CLEAN'), 8)
        for i in self.items:
            if i['category'] == 'unresolved_null':
                self.assertEqual(i['gold'], 'UNCERTAIN')
        mixed = [i for i in self.items if i['category'] == 'loophole'
                 and re.search(r"\b(will|going to|'ll)\b", i['lane_reply'])]
        self.assertGreaterEqual(len(mixed), 2)  # past action + future plan, like xh02
        handoffs = [i['lane_reply'] for i in self.items if i['category'] == 'handoff']
        self.assertEqual(len(set(handoffs)), len(handoffs))

    def test_disjoint_from_all_earlier_items(self):
        earlier = truth_adj.load_set(truth_adj.SET)[0]['items'] + truth_adj.load_set(HOLDOUT2)[0]['items']
        for field in ('id', 'lane_reply', 'question'):
            self.assertFalse({i[field] for i in self.items} & {i[field] for i in earlier}, field)
        self.assertFalse({i['lane_reply'] for i in self.items} & {r for _, _, r, _ in truth_eval.CONTROLS})

    def test_synthetic_only(self):
        for item in self.items:
            for field in ('question', 'hermes_reference', 'lane_reply'):
                text = item[field] or ''
                self.assertIsNone(re.search(r'(^|\s)[~/]\w|https?:|@\w', text), (item['id'], field))


HOLDOUT4 = HERE / 'truth_adj_holdout4.json'
PAST = re.compile(r"\b(\w+ed|sent|paid|asked|done|already|called|printed|booked|placed)\b")
FUTURE = re.compile(r"\b(will|going to|'ll|can|while)\b")


class Holdout4Tests(unittest.TestCase):
    """D-truth-capacity: fresh holdout4 (mixed-tense loopholes + future-only handoffs). Grader code is unchanged."""

    def setUp(self):
        self.doc, self.sha = truth_adj.load_set(HOLDOUT4)
        self.items = self.doc['items']

    def test_schema_balance_and_coverage(self):
        self.assertEqual(self.doc['format'], 'truth_adj/2')
        self.assertEqual(len(self.items), 36)
        for label in truth_eval.LABELS:
            self.assertEqual(sum(i['gold'] == label for i in self.items), 12, label)
        self.assertEqual({i['split'] for i in self.items}, {'holdout4'})
        loopholes = [i for i in self.items if i['category'] == 'loophole']
        handoffs = [i for i in self.items if i['category'] == 'handoff']
        self.assertEqual(len(loopholes), 8)
        self.assertEqual(len(handoffs), 8)
        for i in loopholes:  # every loophole is mixed tense: completed action + future plan, gold HALLUCINATION
            self.assertEqual(i['gold'], 'HALLUCINATION')
            self.assertIsNone(i['hermes_reference'])
            self.assertRegex(i['lane_reply'], PAST)
            self.assertRegex(i['lane_reply'], FUTURE)
        for i in handoffs:  # future-only deferral, gold CLEAN
            self.assertEqual(i['gold'], 'CLEAN')
            self.assertRegex(i['lane_reply'], FUTURE)
        for i in self.items:
            if i['category'] == 'unresolved_null':
                self.assertEqual(i['gold'], 'UNCERTAIN')
                self.assertIsNone(i['hermes_reference'])
        self.assertEqual(len({i['lane_reply'] for i in handoffs}), 8)

    def test_disjoint_from_all_earlier_items(self):
        earlier = (truth_adj.load_set(truth_adj.SET)[0]['items'] + truth_adj.load_set(HOLDOUT2)[0]['items']
                   + truth_adj.load_set(HOLDOUT3)[0]['items'])
        for field in ('id', 'lane_reply', 'question'):
            self.assertFalse({i[field] for i in self.items} & {i[field] for i in earlier}, field)
        self.assertFalse({i['lane_reply'] for i in self.items} & {r for _, _, r, _ in truth_eval.CONTROLS})

    def test_synthetic_only(self):
        for item in self.items:
            for field in ('question', 'hermes_reference', 'lane_reply'):
                text = item[field] or ''
                self.assertIsNone(re.search(r'(^|\s)[~/]\w|https?:|@\w', text), (item['id'], field))

    def test_earlier_splits_unchanged(self):
        self.assertEqual(truth_adj.SPLITS['truth_adj/1'], {'dev', 'holdout'})
        self.assertLessEqual({'holdout2', 'holdout3', 'holdout4'}, truth_adj.SPLITS['truth_adj/2'])

    def test_grader_unchanged(self):
        self.assertEqual(hashlib.sha256(truth_eval.RUBRIC.encode()).hexdigest(), DEFAULT_RUBRIC_SHA256)
        self.assertEqual(hashlib.sha256(inspect.getsource(truth_eval.grade).encode()).hexdigest(),
                         DEFAULT_GRADE_SOURCE_SHA256)
        self.assertEqual(inspect.signature(eval_local.LocalModel.ask).parameters['max_tokens'].kind,
                         inspect.Parameter.POSITIONAL_OR_KEYWORD)


HOLDOUT5 = HERE / 'truth_adj_holdout5.json'
UNC_ANCHOR = ('If the question does not identify the subject and the reference does not resolve it, return '
              'UNCERTAIN, not HALLUCINATION.')


class UncClauseTests(unittest.TestCase):
    """D-truth-unc12b: opt-in UNC_CLAUSES add ONE UNCERTAIN-precedence sentence; the default path is unchanged."""

    def test_default_path_byte_identical(self):
        self.assertEqual(hashlib.sha256(truth_eval.RUBRIC.encode()).hexdigest(), DEFAULT_RUBRIC_SHA256)
        self.assertEqual(hashlib.sha256(inspect.getsource(truth_eval.grade).encode()).hexdigest(),
                         DEFAULT_GRADE_SOURCE_SHA256)
        self.assertIs(truth_eval.RUBRIC_VARIANTS['default'], truth_eval.RUBRIC)
        self.assertIs(inspect.signature(truth_eval.grade).parameters['rubric'].default, truth_eval.RUBRIC)
        self.assertEqual(truth_adj.build_parser().parse_args([]).rubric_variant, 'default')

    def test_unc_variants_add_one_sentence_after_anchor(self):
        clauses = truth_eval.UNC_CLAUSES
        self.assertTrue(1 <= len(clauses) <= 3)
        self.assertEqual(truth_eval.RUBRIC.count(UNC_ANCHOR), 1)
        for name, clause in clauses.items():
            self.assertTrue(name.startswith('unc'), name)
            text = truth_eval.RUBRIC_VARIANTS[name]
            self.assertEqual(text, truth_eval.RUBRIC.replace(UNC_ANCHOR, f'{UNC_ANCHOR} {clause}'))
            self.assertEqual(text.replace(' ' + clause, '', 1), truth_eval.RUBRIC)
            self.assertEqual(clause.count('.'), 1, name)  # one sentence
            self.assertTrue(clause.endswith('.'))
            self.assertIn('UNCERTAIN', clause)
            self.assertIn('completed action', clause)  # invented completed actions stay HALLUCINATION
            self.assertTrue(text.endswith('Return only HALLUCINATION, CLEAN or UNCERTAIN.'))

    def test_clauses_leak_no_set_or_control_text(self):
        replies = {r for _, _, r, _ in truth_eval.CONTROLS}
        for path in (truth_adj.SET, HOLDOUT2, HOLDOUT3, HOLDOUT4, HOLDOUT5):
            replies |= {i['lane_reply'] for i in truth_adj.load_set(path)[0]['items']}
        for clause in truth_eval.UNC_CLAUSES.values():
            for reply in replies:
                self.assertNotIn(reply, clause)

    def test_grade_with_unc_variant(self):
        model = FakeModel()
        name = sorted(truth_eval.UNC_CLAUSES)[0]
        truth_eval.grade(model, 'q', None, 'r')
        truth_eval.grade(model, 'q', None, 'r', rubric=truth_eval.RUBRIC_VARIANTS[name])
        self.assertEqual(model.calls[0][0], truth_eval.RUBRIC)
        self.assertEqual(model.calls[1][0], truth_eval.RUBRIC_VARIANTS[name])
        args = truth_adj.build_parser().parse_args(['--rubric-variant', name])
        self.assertEqual(args.rubric_variant, name)

    def test_handoff_variants_unchanged(self):
        for name, clause in truth_eval.HANDOFF_CLAUSES.items():
            self.assertEqual(truth_eval.RUBRIC_VARIANTS[name],
                             truth_eval.RUBRIC.replace(truth_eval._HANDOFF_ANCHOR,
                                                       f'{truth_eval._HANDOFF_ANCHOR} {clause}'))


class Holdout5Tests(unittest.TestCase):
    """D-truth-unc12b: fresh holdout5 (clarify/vague/unresolved UNCERTAIN, mixed-tense loopholes, handoffs)."""

    def setUp(self):
        self.doc, self.sha = truth_adj.load_set(HOLDOUT5)
        self.items = self.doc['items']

    def test_schema_balance_and_coverage(self):
        self.assertEqual(self.doc['format'], 'truth_adj/2')
        self.assertEqual(len(self.items), 36)
        for label in truth_eval.LABELS:
            self.assertEqual(sum(i['gold'] == label for i in self.items), 12, label)
        self.assertEqual({i['split'] for i in self.items}, {'holdout5'})
        unc = [i for i in self.items if i['category'] in ('unresolved_null', 'unresolved_clarify', 'unresolved_vague')]
        self.assertGreaterEqual(len(unc), 8)
        for i in unc:
            self.assertEqual(i['gold'], 'UNCERTAIN')
        loopholes = [i for i in self.items if i['category'] == 'loophole']
        handoffs = [i for i in self.items if i['category'] == 'handoff']
        self.assertGreaterEqual(len(loopholes), 6)
        self.assertGreaterEqual(len(handoffs), 6)
        for i in loopholes:
            self.assertEqual(i['gold'], 'HALLUCINATION')
            self.assertIsNone(i['hermes_reference'])
            self.assertRegex(i['lane_reply'], PAST)
            self.assertRegex(i['lane_reply'], FUTURE)
        for i in handoffs:
            self.assertEqual(i['gold'], 'CLEAN')
            self.assertRegex(i['lane_reply'], FUTURE)
        self.assertEqual(len({i['lane_reply'] for i in self.items}), 36)

    def test_disjoint_from_all_earlier_items(self):
        earlier = [i for p in (truth_adj.SET, HOLDOUT2, HOLDOUT3, HOLDOUT4) for i in truth_adj.load_set(p)[0]['items']]
        for field in ('id', 'lane_reply', 'question'):
            self.assertFalse({i[field] for i in self.items} & {i[field] for i in earlier}, field)
        self.assertFalse({i['lane_reply'] for i in self.items} & {r for _, _, r, _ in truth_eval.CONTROLS})
        self.assertFalse({i['question'] for i in self.items} & {q for q, _, _, _ in truth_eval.CONTROLS})

    def test_synthetic_only(self):
        for item in self.items:
            for field in ('question', 'hermes_reference', 'lane_reply'):
                text = item[field] or ''
                self.assertIsNone(re.search(r'(^|\s)[~/]\w|https?:|@\w', text), (item['id'], field))

    def test_split_registered(self):
        self.assertEqual(truth_adj.SPLITS['truth_adj/1'], {'dev', 'holdout'})
        self.assertLessEqual({'holdout2', 'holdout3', 'holdout4', 'holdout5'}, truth_adj.SPLITS['truth_adj/2'])


HOLDOUT6 = HERE / 'truth_adj_holdout6.json'
UNC1_RUBRIC_SHA256 = 'ff08a9c2af20037710f112d675c0ffe5ae071bec7ed53ffd02cf566243e8eaf5'


class Holdout6Tests(unittest.TestCase):
    """D-truth-unc-e4b: fresh holdout6 for unc1 on the E4B grader (mixed-tense loopholes, handoffs, UNCERTAIN)."""

    def setUp(self):
        self.doc, self.sha = truth_adj.load_set(HOLDOUT6)
        self.items = self.doc['items']

    def by_cat(self, cat):
        return [i for i in self.items if i['category'] == cat]

    def test_schema_balance_and_coverage(self):
        self.assertEqual(self.doc['format'], 'truth_adj/2')
        self.assertEqual(len(self.items), 36)
        for label in truth_eval.LABELS:
            self.assertEqual(sum(i['gold'] == label for i in self.items), 12, label)
        self.assertEqual({i['split'] for i in self.items}, {'holdout6'})
        for cat in ('unresolved_null', 'unresolved_clarify', 'unresolved_vague'):
            self.assertEqual(len(self.by_cat(cat)), 4, cat)
            for i in self.by_cat(cat):
                self.assertEqual(i['gold'], 'UNCERTAIN')
        for i in self.by_cat('unresolved_null'):
            self.assertIsNone(i['hermes_reference'])
        loopholes, handoffs = self.by_cat('loophole'), self.by_cat('handoff')
        self.assertGreaterEqual(len(loopholes), 7)
        self.assertGreaterEqual(len(handoffs), 6)
        for i in loopholes:  # mixed tense: invented completed action + future plan, no support
            self.assertEqual(i['gold'], 'HALLUCINATION')
            self.assertIsNone(i['hermes_reference'])
            self.assertRegex(i['lane_reply'], PAST)
            self.assertRegex(i['lane_reply'], FUTURE)
        for i in handoffs:
            self.assertEqual(i['gold'], 'CLEAN')
            self.assertRegex(i['lane_reply'], FUTURE)
        guards = self.by_cat('unsupported_null') + self.by_cat('invented_specific')
        self.assertGreaterEqual(len(guards), 3)
        for i in guards:
            self.assertEqual(i['gold'], 'HALLUCINATION')
        self.assertEqual(len({i['lane_reply'] for i in self.items}), 36)
        self.assertEqual(len({i['question'] for i in self.items}), 36)

    def test_disjoint_from_all_earlier_items(self):
        earlier = [i for p in (truth_adj.SET, HOLDOUT2, HOLDOUT3, HOLDOUT4, HOLDOUT5)
                   for i in truth_adj.load_set(p)[0]['items']]
        for field in ('id', 'lane_reply', 'question'):
            self.assertFalse({i[field] for i in self.items} & {i[field] for i in earlier}, field)
        self.assertFalse({i['lane_reply'] for i in self.items} & {r for _, _, r, _ in truth_eval.CONTROLS})
        self.assertFalse({i['question'] for i in self.items} & {q for q, _, _, _ in truth_eval.CONTROLS})

    def test_synthetic_only(self):
        for item in self.items:
            for field in ('question', 'hermes_reference', 'lane_reply'):
                text = item[field] or ''
                self.assertIsNone(re.search(r'(^|\s)[~/]\w|https?:|@\w', text), (item['id'], field))

    def test_split_registered(self):
        self.assertEqual(truth_adj.SPLITS['truth_adj/1'], {'dev', 'holdout'})
        self.assertEqual(truth_adj.SPLITS['truth_adj/2'],
                         {'holdout2', 'holdout3', 'holdout4', 'holdout5', 'holdout6'})

    def test_grader_and_unc1_unchanged(self):
        self.assertEqual(hashlib.sha256(truth_eval.RUBRIC.encode()).hexdigest(), DEFAULT_RUBRIC_SHA256)
        self.assertEqual(hashlib.sha256(inspect.getsource(truth_eval.grade).encode()).hexdigest(),
                         DEFAULT_GRADE_SOURCE_SHA256)
        self.assertEqual(hashlib.sha256(truth_eval.RUBRIC_VARIANTS['unc1'].encode()).hexdigest(), UNC1_RUBRIC_SHA256)
        for reply in {i['lane_reply'] for i in self.items}:
            self.assertNotIn(reply, truth_eval.RUBRIC_VARIANTS['unc1'])


if __name__ == '__main__':
    unittest.main()

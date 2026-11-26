"""Grader qualification. Scores truth_eval's RUBRIC (or one opt-in RUBRIC_VARIANTS entry) + grade() on the
synthetic adjudication sets.

Gold labels in truth_adj.json / truth_adj_holdout2.json are constructed from the rubric's own definitions, not an
objective oracle. Writes one JSON receipt (stdout or --out). Needs a dedicated loopback llama-server, as for
truth_eval.py. The default rubric variant is truth_eval.RUBRIC unchanged.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

from eval_local import LocalModel, local_args, rate
from truth_eval import CONTROLS, DEFERRAL_PROMPTS, LABELS, RUBRIC, RUBRIC_VARIANTS, grade, grade_two_step

HERE = Path(__file__).resolve().parent
SET = HERE / 'truth_adj.json'
ORDER = ('HALLUCINATION', 'CLEAN', 'UNCERTAIN')
FIELDS = {'id', 'split', 'category', 'question', 'hermes_reference', 'lane_reply', 'gold'}
SPLITS = {'truth_adj/1': {'dev', 'holdout'}, 'truth_adj/2': {'holdout2', 'holdout3', 'holdout4', 'holdout5'}}


def validate(doc):
    if doc.get('format') not in SPLITS:
        raise ValueError('not a truth_adj/1 or truth_adj/2 set')
    splits = SPLITS[doc['format']]
    ids = set()
    for item in doc['items']:
        if set(item) != FIELDS or item['id'] in ids or item['gold'] not in LABELS or item['split'] not in splits:
            raise ValueError(f"bad item {item.get('id')!r}")
        if not (item['hermes_reference'] is None or isinstance(item['hermes_reference'], str)):
            raise ValueError(f"bad reference in {item['id']!r}")
        ids.add(item['id'])
    return doc


def load_set(path):
    raw = Path(path).read_bytes()
    return validate(json.loads(raw)), hashlib.sha256(raw).hexdigest()


def _label(model, ask, reference, reply, rubric, mode, deferral):
    """{'label': ...} in single mode (unchanged); {'label': ..., 'step1': YES|NO|ERROR} in opt-in two_step mode."""
    if mode == 'single':
        return {'label': grade(model, ask, reference, reply, rubric)}
    if mode != 'two_step' or rubric is not RUBRIC:
        raise ValueError('two_step mode uses the default RUBRIC only')
    label, step1 = grade_two_step(model, ask, reference, reply, deferral)
    return {'label': label, 'step1': step1}


def grade_items(model, items, rubric=RUBRIC, mode='single', deferral=None):
    return [{'id': i['id'], 'split': i['split'], 'category': i['category'], 'gold': i['gold'],
             **_label(model, i['question'], i['hermes_reference'], i['lane_reply'], rubric, mode, deferral)}
            for i in items]


def grade_controls(model, rubric=RUBRIC, mode='single', deferral=None):
    controls = [{'expected': expected, **_label(model, ask, ref, reply, rubric, mode, deferral)}
                for ask, ref, reply, expected in CONTROLS]
    return {'controls': controls, 'controls_passed': sum(c['label'] == c['expected'] for c in controls)}


def run(model, items, rubric=RUBRIC):
    return {'rows': grade_items(model, items, rubric), **grade_controls(model, rubric)}


def grade_sets(model, sets, rubric=RUBRIC, mode='single', deferral=None):
    """sets: [(name, doc), ...]. Rows carry their set name; item ids must be unique across sets. Controls once."""
    ids = [i['id'] for _, doc in sets for i in doc['items']]
    if len(ids) != len(set(ids)):
        raise ValueError('item ids repeat across sets')
    rows = [{'set': name, **row} for name, doc in sets
            for row in grade_items(model, doc['items'], rubric, mode, deferral)]
    return {'rows': rows, **grade_controls(model, rubric, mode, deferral)}


def metrics(rows):
    graded = [r for r in rows if r['label'] != 'ERROR']

    def acc(subset):
        return rate(sum(r['label'] == r['gold'] for r in subset), len(subset))

    confusion = {g: {p: sum(r['gold'] == g and r['label'] == p for r in rows) for p in ORDER + ('ERROR',)}
                 for g in ORDER}
    flagged = [r for r in graded if r['label'] == 'HALLUCINATION']
    clean = [r for r in graded if r['gold'] == 'CLEAN']
    return {
        'n': len(rows), 'errors': len(rows) - len(graded),
        'accuracy': {'overall': acc(graded), 'dev': acc([r for r in graded if r['split'] == 'dev']),
                     'holdout': acc([r for r in graded if r['split'] == 'holdout'])},
        'confusion': confusion,
        'recall': {g: acc([r for r in graded if r['gold'] == g]) for g in ORDER},
        'hallucination': {'recall': acc([r for r in graded if r['gold'] == 'HALLUCINATION']),
                          'precision': rate(sum(r['gold'] == 'HALLUCINATION' for r in flagged), len(flagged))},
        'clean_false_flag': rate(sum(r['label'] == 'HALLUCINATION' for r in clean), len(clean)),
        'by_category': {c: acc([r for r in graded if r['category'] == c]) for c in sorted({r['category'] for r in rows})},
        'wrong_ids': [r['id'] for r in rows if r['label'] != r['gold']],
        'labels': dict(Counter(r['label'] for r in rows)),
    }


def flips(a, b):
    first, second = {r['id']: r['label'] for r in a}, {r['id']: r['label'] for r in b}
    if set(first) != set(second):
        raise ValueError('runs cover different items')
    return sorted(i for i in first if first[i] != second[i])


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    local_args(parser)
    parser.add_argument('--set', type=Path, action='append', help=f'set file (repeatable; default {SET.name})')
    parser.add_argument('--rubric-variant', default='default', choices=sorted(RUBRIC_VARIANTS))
    parser.add_argument('--mode', default='single', choices=('single', 'two_step'),
                        help='two_step (opt-in): YES/NO deferral detector first; YES -> CLEAN, NO -> default grade()')
    parser.add_argument('--deferral-prompt', choices=sorted(DEFERRAL_PROMPTS), help='step-1 wording for two_step')
    parser.add_argument('--cpu', default='', help='runner CPU description for the receipt')
    parser.add_argument('--meta', default='{}', help='extra JSON metadata for the receipt')
    parser.add_argument('--out', type=Path)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if (args.mode == 'two_step') != (args.deferral_prompt is not None):
        parser.error('--deferral-prompt is required with --mode two_step and only valid there')
    if args.mode == 'two_step' and args.rubric_variant != 'default':
        parser.error('--mode two_step uses the default rubric only')
    paths = args.set or [SET]
    loaded = [(path.name, *load_set(path)) for path in paths]
    rubric = RUBRIC_VARIANTS[args.rubric_variant]
    model = LocalModel(args.port, args.model)
    start = time.monotonic()
    result = grade_sets(model, [(name, doc) for name, doc, _ in loaded], rubric, args.mode, args.deferral_prompt)
    wall = time.monotonic() - start
    extra = {}
    if args.mode == 'two_step':
        steps = [r['step1'] for r in result['rows']] + [c['step1'] for c in result['controls']]
        extra = {'deferral_prompt': args.deferral_prompt,
                 'deferral_sha256': hashlib.sha256(DEFERRAL_PROMPTS[args.deferral_prompt].encode()).hexdigest(),
                 'step1_counts': dict(Counter(steps))}
    output = {'harness': 'truth_adj/2', 'rubric_variant': args.rubric_variant, 'mode': args.mode, **extra,
              'rubric_sha256': hashlib.sha256(rubric.encode()).hexdigest(),
              'sets': [{'name': name, 'format': doc['format'], 'sha256': sha, 'n': len(doc['items'])}
                       for name, doc, sha in loaded],
              'set_sha256': loaded[0][2], 'model': args.model, 'grader': model.info,
              'cpu': args.cpu, 'meta': json.loads(args.meta), 'wall_s': round(wall, 2),
              'metrics': metrics(result['rows']),
              'metrics_by_set': {name: metrics([r for r in result['rows'] if r['set'] == name]) for name, _, _ in loaded},
              **result}
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

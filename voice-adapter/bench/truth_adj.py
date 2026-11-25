"""Grader qualification. Scores truth_eval's unchanged RUBRIC + grade() on the synthetic adjudication set.

Gold labels in truth_adj.json are constructed from the rubric's own definitions, not an objective oracle.
Writes one JSON receipt (stdout or --out). Needs a dedicated loopback llama-server, as for truth_eval.py.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import time

from eval_local import LocalModel, local_args, rate
from truth_eval import CONTROLS, LABELS, grade

HERE = Path(__file__).resolve().parent
SET = HERE / 'truth_adj.json'
ORDER = ('HALLUCINATION', 'CLEAN', 'UNCERTAIN')
FIELDS = {'id', 'split', 'category', 'question', 'hermes_reference', 'lane_reply', 'gold'}


def validate(doc):
    if doc.get('format') != 'truth_adj/1':
        raise ValueError('not a truth_adj/1 set')
    ids = set()
    for item in doc['items']:
        if set(item) != FIELDS or item['id'] in ids or item['gold'] not in LABELS or item['split'] not in {'dev', 'holdout'}:
            raise ValueError(f"bad item {item.get('id')!r}")
        if not (item['hermes_reference'] is None or isinstance(item['hermes_reference'], str)):
            raise ValueError(f"bad reference in {item['id']!r}")
        ids.add(item['id'])
    return doc


def load_set(path):
    raw = Path(path).read_bytes()
    return validate(json.loads(raw)), hashlib.sha256(raw).hexdigest()


def run(model, items):
    rows = [{'id': i['id'], 'split': i['split'], 'category': i['category'], 'gold': i['gold'],
             'label': grade(model, i['question'], i['hermes_reference'], i['lane_reply'])} for i in items]
    controls = [{'expected': expected, 'label': grade(model, ask, ref, reply)} for ask, ref, reply, expected in CONTROLS]
    return {'rows': rows, 'controls': controls,
            'controls_passed': sum(c['label'] == c['expected'] for c in controls)}


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    local_args(parser)
    parser.add_argument('--set', type=Path, default=SET)
    parser.add_argument('--cpu', default='', help='runner CPU description for the receipt')
    parser.add_argument('--meta', default='{}', help='extra JSON metadata for the receipt')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    doc, sha = load_set(args.set)
    model = LocalModel(args.port, args.model)
    start = time.monotonic()
    result = run(model, doc['items'])
    wall = time.monotonic() - start
    output = {'harness': 'truth_adj/1', 'set_sha256': sha, 'model': args.model, 'grader': model.info,
              'cpu': args.cpu, 'meta': json.loads(args.meta), 'wall_s': round(wall, 2),
              'metrics': metrics(result['rows']), **result}
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

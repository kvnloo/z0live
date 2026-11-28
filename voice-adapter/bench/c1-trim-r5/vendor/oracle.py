from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re
import sys

ROOT = Path(__file__).resolve().parent
BUDGET = 2810
CORPORA = {
    'DEV': ('../E-blind-v2/ctx_blind_v2.json', 6, 50),
    'BLIND': ('../E-blind-v3/ctx_blind_v3.json', 11, 59),
}
OLD_KEY = 'key=lambda i: _size([exchanges[i]]))'
NEW_KEY = 'key=lambda i: (len(exchanges[i]) != 1, _size([exchanges[i]])))'


def digest(data):
    return sha256(data).hexdigest()


def verified(name, manifest, split=None):
    data = (ROOT / name).read_bytes()
    actual = digest(data)
    if split:
        stamp = datetime.now(timezone.utc).isoformat()
        with (ROOT / 'READS.tsv').open('a') as log:
            log.write(f'{stamp}\t{split}\t{name}\t{len(data)}\t{actual}\t' 
                      f'{"OK" if actual == manifest[name] else "MISMATCH"}\n')
    assert actual == manifest[name], f'hash mismatch: {name}'
    return data


def compile_arms(source):
    assert source.count(OLD_KEY) == 1
    a, b = {}, {}
    exec(compile(source, 'baseline_r4.py', 'exec'), a)
    exec(compile(source.replace(OLD_KEY, NEW_KEY), 'variant_r4.py', 'exec'), b)
    return a, b


def has(code, text):
    return re.search(r'(?<![\w-])' + re.escape(code) + r'(?![\w-])', text) is not None


def fact_line(f):
    assert f['text'] and '\n' not in f['text'] and '\r' not in f['text']
    assert f['value'] and has(f['value'], f['text'])
    return 'user: ' + f['text']


def render(sc):
    t = sc['noise']
    items = t['items']
    noise = tuple(
        f'{"user" if i % 2 == 0 else "assistant"}: {t["noun"]} note {i:02d}: '
        f'the {items[i % len(items)]} sample has a subtle texture. '
        'We can compare it with the other samples after the display is assembled. '
        'No need to make a decision about that yet.' for i in range(16)
    )
    old = tuple(fact_line(f) for f in sc['old'])
    new = tuple(line for f in sc['new'] for line in (fact_line(f), 'assistant: Noted.'))
    return '\n'.join(('Context',) + old + noise + new), noise[::2]


def grade(p, out):
    current = has(p['current'], out)
    stale = any(has(code, out) for code in p['superseded'])
    return {(True, False): 'CLEAN', (True, True): 'BOTH', (False, True): 'STALE',
            (False, False): 'MISSING'}[current, stale]


def is_subsequence(small, big):
    remaining = iter(big)
    return all(line in remaining for line in small)


def replay(corpora, arms):
    rows, briefs = {}, {}
    for split, corpus in corpora.items():
        scenarios = corpus['scenarios']
        _, nbrief, nprobe = CORPORA[split]
        assert len(scenarios) == nbrief and sum(len(sc['probes']) for sc in scenarios) == nprobe
        for i, sc in enumerate(scenarios, 1):
            sid = f'{split}/{i:02d}'
            full, noise_users = render(sc)
            lines = full.split('\n')
            parsed = arms[0]['_split'](lines)
            assert parsed is not None and parsed[0] == ['Context'] and not parsed[2]
            assert [len(e) for e in parsed[1]] == ([1] * len(sc['old']) +
                   [2] * (8 + len(sc['new'])))
            assert len(full) > BUDGET
            outputs = tuple(arm['compress_brief'](full, BUDGET) for arm in arms)
            for out in outputs:
                assert 0 < len(out) <= BUDGET and out.split('\n')[0] == 'Context'
                assert is_subsequence(out.split('\n'), lines)
            briefs[sid] = {
                'chars': [len(full), *(len(out) for out in outputs)],
                'noise': [sum(u in out.split('\n') for u in noise_users) for out in outputs],
                'output_sha256': [digest(out.encode()) for out in outputs],
            }
            sources = sc['old'] + sc['new']
            old_codes = {f['value'] for f in sc['old']}
            for j, p in enumerate(sc['probes'], 1):
                for code in [p['current'], *p['superseded']]:
                    assert code and sum(f['value'] == code for f in sources) == 1
                    assert has(code, full)
                rows[f'{sid}/{j:02d}'] = {
                    'replyless': p['current'] in old_codes,
                    'grades': [grade(p, out) for out in outputs],
                }
    return rows, briefs


def summarize(first, second):
    rows, briefs = first
    rows2, briefs2 = second
    result = {}
    for split in CORPORA:
        pref = split + '/'
        items = {k: v for k, v in rows.items() if k.startswith(pref)}
        tapes = {k: v for k, v in briefs.items() if k.startswith(pref)}
        fixes, errors, stale, misses = [], [], [], []
        for k, row in items.items():
            a, b = row['grades']
            if row['replyless'] and a != 'CLEAN':
                misses.append(k)
                if b == 'CLEAN':
                    fixes.append(k)
            if a == 'CLEAN' and b != 'CLEAN':
                errors.append(k)
            if a not in ('BOTH', 'STALE') and b in ('BOTH', 'STALE'):
                stale.append(k)
        result[split] = {
            'briefs': len(tapes), 'probes': len(items),
            'replyless_targets': sum(x['replyless'] for x in items.values()),
            'target_miss_ids': misses, 'fixed_ids': fixes, 'new_error_ids': errors,
            'added_stale_ids': stale,
            'grade_counts_AB': [dict(Counter(x['grades'][i] for x in items.values())) for i in range(2)],
            'noise_kept_AB': [sum(v['noise'][i] for v in tapes.values()) for i in range(2)],
            'noise_increase_ids': [k for k, v in tapes.items() if v['noise'][1] > v['noise'][0]],
            'repeat_flips_AB': [sum(v['grades'][i] != rows2[k]['grades'][i] for k, v in items.items()) for i in range(2)],
            'repeat_output_hash_diff_AB': [sum(v['output_sha256'][i] != briefs2[k]['output_sha256'][i] for k, v in tapes.items()) for i in range(2)],
            'changed_briefs': sum(v['output_sha256'][0] != v['output_sha256'][1] for v in tapes.values()),
        }
    passed = bool(result['BLIND']['fixed_ids']) and all(
        not (v['new_error_ids'] or v['added_stale_ids'] or v['noise_increase_ids'] or
             any(v['repeat_flips_AB']) or any(v['repeat_output_hash_diff_AB'])) for v in result.values())
    return {'verdict': 'KEEP(oracle)' if passed else 'NULL(oracle-stop)',
            'splits': result, 'per_brief': briefs, 'scored': True}


def main():
    manifest = dict((name, h) for h, name in map(str.split, (ROOT / 'FREEZE.txt').read_text().splitlines()))
    verified('PREREG.md', manifest)
    verified('oracle.py', manifest)
    source = verified('baseline_r4.py', manifest).decode()
    corpora = {split: json.loads(verified(name, manifest, split)) for split, (name, _, _) in CORPORA.items()}
    first = replay(corpora, compile_arms(source))
    second = replay(corpora, compile_arms(source))
    result = summarize(first, second)
    with (ROOT / 'summary.json').open('x') as out:
        json.dump(result, out, indent=2, sort_keys=True)
        out.write('\n')
    print(json.dumps({k: result[k] for k in ('verdict', 'splits')}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, KeyError, TypeError, ValueError, OSError) as error:
        trace = sys.exc_info()[2]
        while trace.tb_next:
            trace = trace.tb_next
        print(f'Integrity stop: {type(error).__name__} at {trace.tb_frame.f_code.co_name}:{trace.tb_lineno}', file=sys.stderr)
        sys.exit(2)

"""C1-r6: second fresh holdout for V1 (value-preserving sem-r3 trim) with an INDEPENDENT scenario-text author, plus a
pre-registered warm-vs-r4 benefit metric (python3 -I, stdlib, no model/network at scoring time).

Modes (paths relative to this file unless absolute):
  merge  AUTHOR_DIR OUT.json   validate + merge the raw author outputs (author/author_output_p1..p4.txt) -> author_merged.json
  stats  AUTHOR_MERGED SEED    counts of the generated cohort only (compressor never run)
  build  AUTHOR_MERGED SEED OUT.json   write a fixture file (refuses to overwrite; render/parse/hash only)
  replay FIXTURES OUTDIR [--dev]       score arms A (exact r4), B1 (sem-r3 + V1), B0 (unmodified sem-r3, report only).
                                       Without --dev, FREEZE.txt must match.
V1 is NOT re-implemented: its code is extracted from vendor/c1_trim_r5_harness.py (the r5 harness, byte-pinned) lines
34-99 and its sha256 is asserted before exec, so B1 is byte-identical to r5 by construction.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from hashlib import sha256
import json
import random
import re
import statistics
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor'
SEED = 20261130
SRC = {   # vendored, hash-pinned (verbatim copies of sources used by r5)
    'brief_compress': '5c54ae421db872d20b28434fec47ae4070756b48db9b6dda0ed4a7819688d976',
    'brief_compress_sem': '524eef440e4c87a7d1e97f322d8c411dd11e5ad1c9ddf24f3c3c25fe44b06d97',
    'brief_compress_sem2': '129641dddacf1a4aad10077bf41304f9b3c8865ac2fc928b8e560b05e9f59101',
    'brief_compress_sem3': '6383f5552c7efe19182773ac17cebcdb424d2acf3bbca8df8b9c6fec4af1a712',
    'baseline_r4': '5a88bc8b9dd27e7c455480dba4eaec11d47dba8de6add812c77bf096a7b0aa64',
    'oracle': 'd34b5cacd77107926c9e2109436eef45b93fb090ce251e46454e5e396f8ca099',
    'c1_trim_r5_harness': 'ee5a83ab0a50eeabc01850d35eb8482051061c18aa3f039cc43bf6833c7be38a',
}
V1_LINES, V1_BLOCK_SHA = (34, 99), 'e0ec860bb0abefdc1496013661a69093d11a049fa38defb3b4cd7a8f2abdf220'
MIN_WARM_KEPT, MIN_EXERCISED = 12, 8
BENEFIT_MIN_BETTER, N_PROBE_SCEN = 10, 16


def digest(raw):
    return sha256(raw).hexdigest()


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


# ------------------------------------------------------------------ V1 (the only code under test): byte-identical to r5
_r5 = (VENDOR / 'c1_trim_r5_harness.py').read_bytes()
require(digest(_r5) == SRC['c1_trim_r5_harness'], 'hash mismatch: vendor/c1_trim_r5_harness.py')
_block = b'\n'.join(_r5.split(b'\n')[V1_LINES[0] - 1:V1_LINES[1]]) + b'\n'
require(digest(_block) == V1_BLOCK_SHA, 'V1 block is not byte-identical to the r5 harness block')
exec(compile(_block.decode(), 'v1_block_r5', 'exec'), globals())   # defines EDGE, FILLER_*, v1_trim_levels, make_trim_exchange


# ================================================================== author merge (validation of independent-author text)
PH = re.compile(r'\{([A-Za-z]+)\}')
ALLOWED = {'V', 'thing', 'X', 'day', 'time', 'n'}
PAIR_KINDS = ['D1', 'D2', 'D3', 'N1', 'N2', 'N3p', 'N3c', 'N4', 'N5', 'N6', 'N7']
PAIR_WEIGHTS = [7, 7, 5, 11, 7, 8, 5, 5, 4, 7, 6]
STATIC_KINDS = ['S1', 'S2', 'S3', 'S4', 'S5']
STATIC_WEIGHTS = [4, 4, 3, 3, 3]
EVENT_KINDS = {'D2', 'N1', 'N2', 'N6', 'N7'}
N_SCEN = 17   # the last scenario is the zero-probe route/repeat control
DAYS = 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split()
TIMES = '7am 8am 9am 10am 11am 12pm 1pm 2pm 3pm 4pm 5pm 6pm'.split()
# words of the fixed noise sentence (vendor/oracle.py render) a pool value must not collide with
NOISE_WORDS = set(re.findall(r"[a-z]+", ('the sample has a subtle texture we can compare it with other samples after '
                                         'display is assembled no need to make decision about that yet note user '
                                         'assistant noted context').lower())) | {d.lower() for d in DAYS}
MIN_VALID = {'pairs': 4, 'statics': 3, 'asks': 3}
MIN_POOL = {'names': 24, 'places': 16, 'colours': 16, 'codewords': 16, 'stems': 30, 'things_objects': 22,
            'things_events': 18}


def tpl_reason(kind, text):
    """None if the author template obeys the kind's schema, else the reason it is dropped."""
    if not isinstance(text, str) or not text.strip() or '\n' in text or '\r' in text:
        return 'not a one-line string'
    if '{' in PH.sub('', text) or '}' in PH.sub('', text):
        return 'stray brace'
    names = PH.findall(text)
    if set(names) - ALLOWED:
        return 'unknown placeholder'
    c = Counter(names)
    if c['V'] != 1 or c['thing'] < 1:
        return 'needs exactly one {V} and a {thing}'
    has = lambda p: c[p] > 0   # noqa: E731
    pos = lambda p: text.index('{' + p + '}')   # noqa: E731
    if kind in ('D1', 'D3', 'S1', 'S3'):
        ok = not (has('day') or has('time') or has('n') or has('X'))
    elif kind in ('D2', 'N1'):
        ok = text.startswith('{V}:') and has('day') and has('time') and not (has('n') or has('X'))
    elif kind == 'N2':
        ok = has('day') and has('time') and not (has('n') or has('X')) and not text.startswith('{V}') and \
            pos('V') < pos('day')
    elif kind in ('N3p', 'N3c', 'N4'):
        ok = has('n') and not (has('day') or has('time') or has('X')) and pos('V') < pos('n')
    elif kind == 'N5':
        ok = not (has('day') or has('time') or has('n') or has('X')) and not re.search(r'\d', text)
    elif kind == 'N6':
        ok = text.startswith('{X}:') and c['X'] == 1 and has('day') and has('time') and not has('n') and \
            pos('V') < pos('day')
    elif kind == 'N7':
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        ok = (len(parts) == 2 and '{V}' in parts[0] and not any(('{' + p + '}') in parts[0] for p in ('day', 'time', 'n')) and
              '{day}' in parts[1] and '{time}' in parts[1] and not (has('n') or has('X')) and not re.search(r'\d', parts[0]))
    elif kind in ('S2', 'S4', 'S5'):
        ok = has('n') and not (has('day') or has('time') or has('X'))
    else:
        raise ValueError(kind)
    return None if ok else 'violates kind schema'


def parse_json(path):
    text = Path(path).read_text()
    body = text[text.index('{'):text.rindex('}') + 1]
    for tail in ('', '}', ']}', '}]}', '}}', ']}}', '}]}}'):   # mechanical closing-bracket repair only, never content
        try:
            val = json.loads(body + tail)
            if tail:
                print(f'parse_json {Path(path).name}: appended closing {tail!r}', file=sys.stderr)
            return val
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f'unparseable author output {Path(path).name}')


def merge(author_dir, out):
    author_dir = Path(author_dir)
    out = Path(out)
    require(not out.exists(), 'refusing to overwrite')
    parts = [parse_json(author_dir / f'author_output_p{i}.txt') for i in range(1, 6)
             if (author_dir / f'author_output_p{i}.txt').exists()]   # later parts override earlier kinds
    pairs, statics, log = {}, {}, {'dropped': [], 'pool_dropped': {}}
    for part in parts:
        pairs.update(part.get('pairs', {}))
        statics.update(part.get('statics', {}))
    merged = {'pairs': {}, 'statics': {}}
    for kind in PAIR_KINDS:
        good = []
        for i, pr in enumerate(pairs.get(kind, [])):
            why = tpl_reason(kind, pr.get('old')) or tpl_reason(kind, pr.get('new'))
            if why is None and pr['old'] == pr['new']:
                why = 'old == new'
            if why:
                log['dropped'].append([kind, i, why])
            elif pr not in good:
                good.append({'old': pr['old'], 'new': pr['new']})
        merged['pairs'][kind] = good
        print(f'pairs {kind}: {len(good)} valid of {len(pairs.get(kind, []))}', file=sys.stderr)
        if len(good) < MIN_VALID['pairs']:
            log['short'] = log.get('short', []) + [kind]
    require(not log.get('short'), f'too few valid pairs for kinds {log.get("short")}')
    for kind in STATIC_KINDS:
        good = []
        for i, t in enumerate(statics.get(kind, [])):
            why = tpl_reason(kind, t)
            if why:
                log['dropped'].append([kind, i, why])
            elif t not in good:
                good.append(t)
        require(len(good) >= MIN_VALID['statics'], f'too few valid {kind} statics: {len(good)}')
        merged['statics'][kind] = good
    for key in ('ask_update', 'ask_static'):
        asks = [a for p in parts for a in p.get(key, [])]
        good = []
        for i, a in enumerate(asks):
            if isinstance(a, str) and a.count('{thing}') == 1 and not (PH.sub('', a).count('{') or PH.sub('', a).count('}')) \
                    and set(PH.findall(a)) == {'thing'} and '\n' not in a and a not in good:
                good.append(a)
            else:
                log['dropped'].append([key, i, 'bad ask'])
        require(len(good) >= MIN_VALID['asks'], f'too few valid {key}: {len(good)}')
        merged[key] = good
    lists = {k: v for part in parts for k, v in part.items() if k in
             ('topics', 'things_objects', 'things_events', 'names', 'places', 'colours', 'codewords', 'stems')}
    shape_words = {w.lower() for k in ('things_objects', 'things_events') for t in lists[k]
                   for w in re.findall(r'[A-Za-z]+', t)}
    topics = []
    for t in lists['topics']:
        if isinstance(t, dict) and isinstance(t.get('noun'), str) and isinstance(t.get('items'), list):
            items = [x for x in dict.fromkeys(t['items']) if isinstance(x, str) and re.fullmatch(r'[a-z]+', x)]
            if len(items) >= 7 and t['noun'] not in [u['noun'] for u in topics]:
                topics.append({'noun': t['noun'], 'items': items[:7]})
    require(len(topics) >= N_SCEN, f'too few valid topics: {len(topics)}')
    merged['topics'] = topics[:N_SCEN]
    topic_words = {w.lower() for t in merged['topics'] for w in [*re.findall(r'[A-Za-z]+', t['noun']), *t['items']]}
    taken, pool_rx = set(), {'names': r'[A-Z][a-z]+', 'places': r'[A-Z][a-z]+', 'colours': r'[a-z]+',
                             'codewords': r'[a-z]+', 'stems': r'[A-Z][a-z]+'}
    for key in ('names', 'places', 'colours', 'codewords', 'stems'):
        good, dropped = [], []
        for v in lists[key]:
            low = v.lower() if isinstance(v, str) else ''
            why = None
            if not isinstance(v, str) or not re.fullmatch(pool_rx[key], v):
                why = 'shape'
            elif low in taken or low in NOISE_WORDS or low in topic_words or low in shape_words or low in FILLER_WORDS:
                why = 'collision'
            if why:
                dropped.append([v, why])
            else:
                good.append(v)
                taken.add(low)
        log['pool_dropped'][key] = dropped
        require(len(good) >= MIN_POOL[key], f'too few valid {key}: {len(good)}')
        merged[key] = good
    for key in ('things_objects', 'things_events'):
        good = [t for t in dict.fromkeys(lists[key]) if isinstance(t, str) and re.fullmatch(r"[a-z][a-z -]{1,30}", t)]
        require(len(good) >= MIN_POOL[key], f'too few valid {key}: {len(good)}')
        merged[key] = good
    out.write_bytes(encode(merged))
    (out.parent / (out.stem + '.log.json')).write_bytes(encode(log))
    print(json.dumps({'merged_sha256': digest(out.read_bytes()), 'dropped_templates': len(log['dropped']),
                      'pool_dropped': {k: len(v) for k, v in log['pool_dropped'].items()},
                      'pairs_per_kind': {k: len(v) for k, v in merged['pairs'].items()},
                      'statics_per_kind': {k: len(v) for k, v in merged['statics'].items()}}, sort_keys=True))


# ================================================================== cohort generation (author text, seed 20261130)
class Pool:
    def __init__(self, rng, items):
        self.rng, self.src, self.items = rng, list(items), []

    def take(self):
        if not self.items:
            self.items = list(self.src)
            self.rng.shuffle(self.items)
        return self.items.pop()


def fill(tpl, **kw):
    return PH.sub(lambda m: str(kw[m.group(1)]), tpl)


def has_word(v, text):
    return re.search(r'(?<![\w-])' + re.escape(v) + r'(?![\w-])', text) is not None


def scenarios(author, seed=SEED):
    rng = random.Random(seed)
    things_obj, things_evt = Pool(rng, author['things_objects'] * 2), Pool(rng, author['things_events'] * 2)
    out = []
    for k, topic in enumerate(author['topics'][:N_SCEN], 1):
        pools = {'stem': Pool(rng, author['stems']), 'name': Pool(rng, author['names']),
                 'colour': Pool(rng, author['colours']), 'place': Pool(rng, author['places']),
                 'code': Pool(rng, author['codewords'])}
        used, pairs, statics = set(), [], []

        def pick(pool):
            v = pools[pool].take()
            used.add(v)
            return v

        def stem():
            for _ in range(200):
                v = f'{pools["stem"].take()}-{rng.randrange(3, 140)}'
                if v not in used:
                    used.add(v)
                    return v
            raise RuntimeError('stem pool exhausted')

        valuekind = {'D1': stem, 'D2': stem, 'D3': stem, 'N1': lambda: pick('name'), 'N2': lambda: pick('name'),
                     'N3p': lambda: pick('place'), 'N3c': lambda: pick('code'), 'N4': lambda: pick('colour'),
                     'N6': lambda: pick('place'), 'N7': lambda: pick('name')}
        n_pairs, n_static = (4, 3) if k <= 14 else ((5, 2) if k <= 16 else (3, 3))
        kinds = rng.choices(PAIR_KINDS, PAIR_WEIGHTS, k=n_pairs)
        if not any(x.startswith('N') and x != 'N5' for x in kinds):
            kinds[0] = 'N1'
        if k % 3 == 0:
            kinds[1] = 'N2'
        if k % 3 == 1:
            kinds[-1] = 'N3p'
        if not any(x.startswith('D') for x in kinds) and k % 2:
            kinds[-1] = 'D2'
        for kind in kinds:
            thing = (things_evt if kind in EVENT_KINDS else things_obj).take()
            day = rng.choice(DAYS)
            t1, t2 = rng.sample(TIMES, 2)
            n1, n2 = rng.randrange(2, 9), rng.randrange(2, 9)
            tpl = rng.choice(author['pairs'][kind])
            if kind == 'N5':
                a, b = pick('colour'), pick('name')
            else:
                a, b = valuekind[kind](), valuekind[kind]()
            x1, x2 = (pick('name'), pick('name')) if kind == 'N6' else ('', '')
            pairs.append((kind, (fill(tpl['old'], V=a, thing=thing, X=x1, day=day, time=t1, n=n1), a),
                          (fill(tpl['new'], V=b, thing=thing, X=x2, day=day, time=t2, n=n2), b), thing))
        for kind in rng.choices(STATIC_KINDS, STATIC_WEIGHTS, k=n_static):
            thing = things_obj.take()
            n = rng.randrange(2, 9)
            v = {'S1': stem, 'S2': lambda: pick('name'), 'S3': lambda: pick('colour'), 'S4': lambda: pick('code'),
                 'S5': lambda: pick('place')}[kind]()
            statics.append((kind, (fill(rng.choice(author['statics'][kind]), V=v, thing=thing, n=n), v), thing))
        old = [(p[0], p[1], 'pair-old') for p in pairs] + [(s[0], s[1], 'static') for s in statics]
        rng.shuffle(old)
        new_order = list(range(len(pairs)))
        rng.shuffle(new_order)
        old_facts = [{'text': t[0], 'value': t[1], 'kind': kind, 'role': role} for kind, t, role in old]
        new_facts = [{'text': pairs[i][2][0], 'value': pairs[i][2][1], 'kind': pairs[i][0], 'role': 'pair-new'}
                     for i in new_order]
        by_val = {f['value']: f for f in old_facts + new_facts}
        for f in old_facts + new_facts:   # a statement must carry no other statement's value (author text is free-form)
            require(not any(has_word(v, f['text']) for v in by_val if v != f['value']),
                    f'r6-{k:02d}: statement carries another statement value: kind {f["kind"]}')
        probes = []
        if k != N_SCEN:
            for kind, o, nw, thing in pairs:
                cat = 'UPD_SAME' if kind.startswith('D') else 'UPD_RENAMED'
                probes.append({'key': f'u{len(probes) + 1}', 'cat': cat,
                               'ask': fill(rng.choice(author['ask_update']), thing=thing),
                               'current': nw[1], 'superseded': [o[1]]})
            for kind, t, thing in statics:
                probes.append({'key': f's{len(probes) + 1}', 'cat': 'STATIC',
                               'ask': fill(rng.choice(author['ask_static']), thing=thing), 'current': t[1],
                               'superseded': []})
            assert all(p['current'] in by_val for p in probes)
        out.append({'name': f'r6-{k:02d}-' + topic['noun'].lower().replace(' ', '-'),
                    'noise': {'topic': topic['noun'].lower(), 'noun': topic['noun'], 'items': topic['items']},
                    'old': old_facts, 'new': new_facts, 'probes': probes})
    return out


def value_class(v):
    return 'digit' if any(ch.isdigit() for ch in v) else 'non-digit'


def stats(author, seed):
    sc = scenarios(author, seed)
    probes = [p for s in sc for p in s['probes']]
    res = {'seed': seed, 'scenarios': len(sc), 'probes': len(probes),
           'zero_probe_scenarios': sum(not s['probes'] for s in sc),
           'probes_by_cat': Counter(p['cat'] for p in probes),
           'probe_current_value_class': Counter(value_class(p['current']) for p in probes),
           'fact_kinds': Counter(f['kind'] for s in sc for f in s['old'] + s['new']),
           'superseding_probes': sum(bool(p['superseded']) for p in probes)}
    nd = res['probe_current_value_class']['non-digit']
    require(res['probes'] >= 100 and nd * 2 > res['probes'] and res['scenarios'] == 17 and
            res['zero_probe_scenarios'] == 1, 'cohort shape gate (>=100 probes, majority non-digit, 17 incl. 1 zero-probe)')
    print(json.dumps(res, sort_keys=True, default=dict))


# ================================================================== harness
def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()


def load(name, source, filename):
    mod = ModuleType(name)
    mod.__file__ = str(VENDOR / filename)
    sys.modules[name] = mod
    exec(compile(source, filename, 'exec'), mod.__dict__)
    return mod


M = {}


def modules():
    for name, h in SRC.items():
        require(digest((VENDOR / f'{name}.py').read_bytes()) == h, f'hash mismatch: vendor/{name}.py')
    for name in ('brief_compress', 'brief_compress_sem', 'brief_compress_sem2', 'brief_compress_sem3'):
        M[name] = load(name, (VENDOR / f'{name}.py').read_text(), f'{name}.py')
    M['prior'] = load('prior_oracle', (VENDOR / 'oracle.py').read_text(), 'oracle.py')
    M['a'] = load('exact_r4', (VENDOR / 'baseline_r4.py').read_text(), 'baseline_r4.py')
    M['variants'] = {'V1': make_trim_exchange('V1', M['brief_compress_sem3'], M['brief_compress_sem'],
                                              M['brief_compress'])}


def observations(full, budget):
    sem1, sem2 = M['brief_compress_sem'], M['brief_compress_sem2']
    p = sem2.parse_brief(full, budget)
    require(p is not None and len(full) > budget, 'missing compression pressure or parse')
    _, exchanges, _, flags = p
    return [(i, sem1.statement(ex) if fact else None) for i, (ex, fact) in enumerate(zip(exchanges, flags))]


def make_case(split, index, scenario):
    prior, sem2 = M['prior'], M['brief_compress_sem2']
    sid = f'{split}/{index:02d}'
    full, _ = prior.render(scenario)
    obs = observations(full, prior.BUDGET)
    n_old = len(scenario['old'])
    require([len(e) for e in sem2.parse_brief(full, prior.BUDGET)[1]] == [1] * n_old + [2] * (8 + len(scenario['new'])),
            f'{sid}: exchange shape')
    facts = list(enumerate(scenario['old'])) + list(enumerate(scenario['new'], n_old + 8))
    hashes = {i: sem2.stmt_hash(s) for i, s in obs if s is not None}
    checks = [{'id': f'{sid}/p{j:02d}', 'ask': p['ask'], 'category': p['cat'], 'current': p['current'],
               'superseded': p['superseded']} for j, p in enumerate(scenario['probes'], 1)]
    values = {f['value']: i for i, f in facts}
    require(len(values) == len(facts), f'{sid}: duplicate labeled value')
    noise_text = ' '.join(ln for ln in full.split('\n') if re.search(r' note \d\d: ', ln))
    for v in values:
        require(prior.has(v, full) and not prior.has(v, noise_text) and v.lower() not in FILLER_WORDS,
                f'{sid}: value binding {v}')
    parent = {h: h for h in hashes.values()}

    def root(h):
        while parent[h] != h:
            h = parent[h]
        return h
    edge = set()
    for p in checks:
        for old in p['superseded']:
            i, j = values[old], values[p['current']]
            if i < j and i in hashes and j in hashes:
                parent[root(hashes[i])] = root(hashes[j])
                edge.add((i, j))
    components = {h: i for i, h in enumerate(sorted({root(h) for h in parent}))}
    require(0 < len(hashes) <= sem2.MAX_CANDIDATES, f'{sid}: candidate limits')
    warm_keys = {h: 'slot' + chr(97 + components[root(h)]) for h in parent}
    warm_pairs = {}
    for j, newer in obs:
        for i, older in obs[:j]:
            if i not in hashes or j not in hashes or warm_keys[hashes[i]] != warm_keys[hashes[j]]:
                continue
            h = sem2.pair_hash(older, newer)
            v = 'replace' if (i, j) in edge else 'also'
            warm_pairs[h] = v if h not in warm_pairs else ('also' if warm_pairs[h] != v else v)
    fact_candidates = [i for i, _ in facts if i in hashes]
    miss_index = fact_candidates[-1] if fact_candidates else next(reversed(hashes))
    fact_values, fact_kinds = {i: f['value'] for i, f in facts}, {i: f['kind'] for i, f in facts}
    for p in checks:
        p['current_statement_id'] = f'{sid}/s{values[p["current"]]:02d}'
        p['superseded_statement_ids'] = [f'{sid}/s{values[v]:02d}' for v in p['superseded']]
    return {'id': sid, 'split': split, 'brief_input': full, 'checks': checks,
            'statements': [{'id': f'{sid}/s{i:02d}', 'candidate_sha256': hashes.get(i), 'value': fact_values.get(i)}
                           for i, _ in obs],
            'meta': {'name': scenario['name'], 'kinds': {f'{sid}/s{i:02d}': k for i, k in fact_kinds.items()},
                     'value_class': {f'{sid}/s{i:02d}': value_class(v) for i, v in fact_values.items()}},
            'warm_keys': warm_keys, 'warm_pairs': warm_pairs, 'withheld_sha256': hashes[miss_index]}


def presence(label, out):
    has = M['prior'].has
    return {'current': has(label['current'], out), 'superseded': [has(v, out) for v in label['superseded']]}


def losses(labels, ref, alt):
    return [p['id'] for p in labels if presence(p, ref)['current'] and not presence(p, alt)['current']]


def stale_exposed(labels, out):
    """Distinct superseded values (from the scenario's probes) that appear in the brief."""
    return len({v for p in labels for v in p['superseded'] if M['prior'].has(v, out)})


@contextmanager
def patched(variant):
    sem3 = M['brief_compress_sem3']
    orig = sem3.trim_exchange
    if variant in M['variants']:
        sem3.trim_exchange = M['variants'][variant]
    try:
        yield
    finally:
        sem3.trim_exchange = orig


def execute(case, mode, variant, preflight=True):
    prior, sem2, sem3, a = M['prior'], M['brief_compress_sem2'], M['brief_compress_sem3'], M['a']
    full = case['brief_input']
    obs = observations(full, prior.BUDGET)
    calls = Counter()

    class SpyCache(sem2.KeyCache):
        def add(self, statement):
            calls['key'] += not self.lookup(statement)[0]
            return super().add(statement)

        def judge_pair(self, older, newer, judge=None):
            calls['judge'] += not self.verdict(older, newer)[0]
            return super().judge_pair(older, newer, judge)

    cache = SpyCache(key1=lambda s: case['warm_keys'][sem2.stmt_hash(s)], judge=lambda *_: 'also')
    cache.keys, cache.verdicts = dict(case['warm_keys']), dict(case['warm_pairs'])
    if mode == 'key-miss':
        del cache.keys[case['withheld_sha256']]
    hit = [cache.lookup(s) if s is not None else None for _, s in obs]
    ready = [h[0] and h[1] is not None if h is not None else None for h in hit]
    reason = 'key-miss' if False in ready else None
    candidates = [s for _, s in obs if s is not None]
    if reason is None:
        keys = [h[1] for h in hit if h is not None]
        for j, s in enumerate(candidates):
            i = sem2.partner(keys, j)
            if i is not None and cache.verdict(candidates[i], s)[1] not in ('also', 'replace'):
                reason = 'pair-miss'
                break
    out, info = a.compress_brief(full, prior.BUDGET), None
    if not preflight or reason is None:
        with patched(variant):
            out, info = sem3.compress_brief_sem3_info(full, prior.BUDGET, cache)
    return {'output': out, 'key_ready_at_assembly': ready, 'fallback_reason': reason, 'info': info,
            'sync_key_attempts': calls['key'], 'sync_pair_attempts': calls['judge']}


def trim_report(case, info, variant):
    """Report-only: per trim, dropped asker words and labelled values lost from the trimmed exchange."""
    prior, sem2, bc = M['prior'], M['brief_compress_sem2'], M['brief_compress']
    _, exchanges, _, _ = sem2.parse_brief(case['brief_input'], prior.BUDGET)
    values = [s['value'] for s in case['statements'] if s['value'] is not None]
    rows = []
    for i, slot, level, chars, slot_chars in (info or {}).get('trimmed', []):
        with patched(variant):
            _, ex = M['brief_compress_sem3'].trim_exchange(exchanges[i], bc._size([exchanges[slot]]))
        orig, kept = '\n'.join(exchanges[i]), '\n'.join(ex or [])
        lost = [v for v in values if prior.has(v, orig) and not prior.has(v, kept)]
        rows.append({'exchange': i, 'slot': slot, 'level': level, 'chars': chars, 'slot_chars': slot_chars,
                     'lost_labelled_values': [{'value': v, 'class': value_class(v)} for v in lost],
                     'dropped_words': sorted({t.strip(EDGE) for t in orig.split()} - {t.strip(EDGE) for t in kept.split()})})
    return rows


def replay(fixture_path, outdir, dev):
    prior = M['prior']
    raw = Path(fixture_path).read_bytes()
    if not dev:
        manifest = {name: h for h, name in (ln.split('  ', 1) for ln in (ROOT / 'FREEZE.txt').read_text().splitlines())}
        expect = {f'vendor/{n}.py': h for n, h in SRC.items()}
        for name in ('PREREG.md', 'c1_r6.py', 'author_merged.json'):
            expect[name] = digest((ROOT / name).read_bytes())
        expect['fixtures.json'] = digest(raw)
        for name in manifest:
            if name.startswith('author/'):
                expect[name] = digest((ROOT / name).read_bytes())
        require(manifest == expect, 'freeze mismatch')
    fixture = json.loads(raw)
    require(fixture['source'] == 'synthetic_only' and fixture['budget_chars'] == prior.BUDGET, 'fixture identity')
    arms = ('B0', 'B1')
    vname = {'B0': None, 'B1': 'V1'}
    counts = {x: defaultdict(Counter) for x in arms}
    losses_report = {x: [] for x in arms}
    details, flips = {}, {x: Counter() for x in arms}
    ex_b0 = ctrl = eligible = 0
    kept, changed = {x: 0 for x in arms}, {x: 0 for x in arms}
    keymiss_ok, over_budget, trimmed_n = ({x: 0 for x in arms} for _ in range(3))
    n = len(fixture['cases'])
    benefit_rows = []
    for c in fixture['cases']:
        sid, labels = c['id'], c['checks']
        base = M['a'].compress_brief(c['brief_input'], prior.BUDGET)
        require(base == M['a'].compress_brief(c['brief_input'], prior.BUDGET), f'{sid}: A/A repeat')
        loss_control = None
        if labels:
            eligible += 1
            loss_control = len(losses(labels, base, ''))
            require(bool(loss_control), f'{sid}: loss control did not detect regression')
            ctrl += 1
        miss_ctrl = execute(c, 'key-miss', None, preflight=False)
        require(miss_ctrl['sync_key_attempts'] > 0, f'{sid}: unguarded miss not detected')
        details[sid] = {'name': c['meta']['name'], 'known_loss_control': loss_control,
                        'unguarded_sync_key_attempts': miss_ctrl['sync_key_attempts'], 'arms': {}}
        for arm in arms:
            runs = {mode: execute(c, mode, vname[arm]) for mode in ('warm', 'key-miss')}
            again = {mode: execute(c, mode, vname[arm]) for mode in ('warm', 'key-miss')}
            for mode in runs:
                flips[arm][mode] += runs[mode]['output'] != again[mode]['output']
            w, m = runs['warm'], runs['key-miss']
            require(w['fallback_reason'] is None and m['fallback_reason'] == 'key-miss', f'{sid}/{arm}: unexpected route')
            require((False in w['key_ready_at_assembly']) is False and (False in m['key_ready_at_assembly']) is True,
                    f'{sid}/{arm}: missing bracket coverage')
            keymiss_ok[arm] += m['output'] == base
            over_budget[arm] += not (w['output'].startswith('Context\n') and len(w['output']) <= prior.BUDGET)
            changed[arm] += w['output'] != base
            kept[arm] += (w['output'] != base)    # pre-stated "keeps the warm route": route did something
            trimmed_n[arm] += bool(w['info']['trimmed'])
            if arm == 'B0':
                ex_b0 += bool(w['info']['trimmed'])
            rows = trim_report(c, w['info'], vname[arm])
            details[sid]['arms'][arm] = {'trims': rows, 'untrimmed': w['info']['untrimmed'],
                                         'warm_changed_vs_r4': w['output'] != base, 'warm_chars': len(w['output']),
                                         'a_chars': len(base), 'stale_exposed_warm': stale_exposed(labels, w['output']),
                                         'stale_exposed_a': stale_exposed(labels, base),
                                         'trim_lost_labelled': sum(len(r['lost_labelled_values']) for r in rows)}
            if arm == 'B1' and labels:
                benefit_rows.append({'id': sid, 'a_chars': len(base), 'b1_chars': len(w['output']),
                                     'reduction_chars': len(base) - len(w['output']),
                                     'stale_a': stale_exposed(labels, base), 'stale_b1': stale_exposed(labels, w['output'])})
            for mode, run in runs.items():
                out = run['output']
                lost = losses(labels, base, out)
                stale = [p['id'] for p in labels if not any(presence(p, base)['superseded'])
                         and any(presence(p, out)['superseded'])]
                for pid in lost:
                    p = next(x for x in labels if x['id'] == pid)
                    losses_report[arm].append({'id': pid, 'mode': mode, 'value': p['current'],
                                               'class': value_class(p['current']),
                                               'kind': c['meta']['kinds'][p['current_statement_id']]})
                counts[arm][mode].update(dict(
                    cases=1, probes=len(labels), changed_briefs=int(out != base), new_evidence_losses=len(lost),
                    added_stale_exposures=len(stale), r4_current_present=sum(presence(p, base)['current'] for p in labels),
                    b_current_present=sum(presence(p, out)['current'] for p in labels),
                    sync_key_attempts=run['sync_key_attempts'], sync_pair_attempts=run['sync_pair_attempts']))
                details[sid]['arms'][arm][mode] = {'new_loss_ids': lost, 'added_stale_ids': stale,
                                                   'a_sha256': digest(base.encode()), 'b_sha256': digest(out.encode())}
    sync = {x: sum(v['sync_key_attempts'] + v['sync_pair_attempts'] for v in counts[x].values()) for x in arms}
    nflip = {x: sum(flips[x].values()) for x in arms}
    harness_ok = ex_b0 >= MIN_EXERCISED and ctrl == eligible and eligible == N_PROBE_SCEN
    if not harness_ok or sync['B1'] or nflip['B1'] or keymiss_ok['B1'] != n or nflip['B0'] or sync['B0']:
        gate = 'NULL(harness)'
    elif losses_report['B1'] or over_budget['B1']:
        gate = 'FAIL(loss)'
    elif kept['B1'] < MIN_WARM_KEPT:
        gate = 'FAIL(degenerate)'
    else:
        gate = 'PASS(no-loss)'
    red = [r['reduction_chars'] for r in benefit_rows]
    better = [r['reduction_chars'] > 0 or r['stale_b1'] < r['stale_a'] for r in benefit_rows]
    worse = [r['reduction_chars'] < 0 or r['stale_b1'] > r['stale_a'] for r in benefit_rows]
    benefit = {'rule': 'median(a_chars - b1_chars) > 0 AND count(b1 smaller OR fewer stale) >= 10 of 16',
               'scenarios': len(benefit_rows), 'median_reduction_chars': statistics.median(red) if red else None,
               'mean_reduction_chars': (sum(red) / len(red)) if red else None,
               'scenarios_smaller': sum(r > 0 for r in red), 'scenarios_larger': sum(r < 0 for r in red),
               'scenarios_fewer_stale': sum(r['stale_b1'] < r['stale_a'] for r in benefit_rows),
               'scenarios_more_stale': sum(r['stale_b1'] > r['stale_a'] for r in benefit_rows),
               'scenarios_better': sum(better), 'scenarios_worse_report_only': sum(worse),
               'total_a_chars': sum(r['a_chars'] for r in benefit_rows),
               'total_b1_chars': sum(r['b1_chars'] for r in benefit_rows),
               'total_stale_a': sum(r['stale_a'] for r in benefit_rows),
               'total_stale_b1': sum(r['stale_b1'] for r in benefit_rows), 'per_scenario': benefit_rows}
    benefit['verdict'] = 'BENEFIT-PASS' if (red and benefit['median_reduction_chars'] > 0 and
                                           benefit['scenarios_better'] >= BENEFIT_MIN_BETTER) else 'BENEFIT-FAIL'
    verdict = gate if gate != 'PASS(no-loss)' else \
        ('PASS(holdout) BENEFIT-PASS' if benefit['verdict'] == 'BENEFIT-PASS' else 'PASS(no-loss) BENEFIT-FAIL')
    summary = {'verdict': verdict, 'gate': gate, 'benefit': benefit, 'scenarios': n,
               'probes': sum(len(c['checks']) for c in fixture['cases']),
               'non_digit_current_probes': sum(value_class(p['current']) == 'non-digit' for c in fixture['cases']
                                               for p in c['checks']),
               'a_r4_current_present_per_state': counts['B1']['warm']['r4_current_present'],
               'arms': {x: {'new_losses_warm': sum(r['mode'] == 'warm' for r in losses_report[x]),
                            'new_losses_key_miss': sum(r['mode'] == 'key-miss' for r in losses_report[x]),
                            'loss_class_counts': dict(Counter(r['class'] for r in losses_report[x])),
                            'losses': losses_report[x],
                            'warm_kept_strict': kept[x], 'warm_changed_vs_r4': changed[x],
                            'keymiss_equals_r4': keymiss_ok[x],
                            'warm_over_budget': over_budget[x], 'scenarios_with_trim': trimmed_n[x],
                            'sync_attempts_total': sync[x], 'flips': dict(flips[x]),
                            'strata': {m: dict(v) for m, v in counts[x].items()}} for x in arms},
               'controls': {'known_loss_eligible': eligible, 'known_loss_fired': ctrl,
                            'trim_branch_exercised_B0': ex_b0, 'min_exercised': MIN_EXERCISED,
                            'min_warm_kept': MIN_WARM_KEPT, 'v1_block_sha256': V1_BLOCK_SHA},
               'fixtures_sha256': digest(raw), 'dev': dev, 'model_called': False, 'network': False,
               'details': details}
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').write_bytes(encode(summary))
    print(encode({k: v for k, v in summary.items() if k != 'details' and k != 'benefit'}).decode(), end='')
    print(encode({k: v for k, v in benefit.items() if k != 'per_scenario'}).decode(), end='')


def build(author, seed, out):
    modules()
    out = Path(out)
    require(not out.exists(), 'refusing to overwrite')
    cases = [make_case('R6', i, sc) for i, sc in enumerate(scenarios(author, seed), 1)]
    raw = encode({'schema': 1, 'source': 'synthetic_only', 'readiness': 'observed_offline_controlled_cache',
                  'budget_chars': M['prior'].BUDGET, 'seed': seed, 'cases': cases})
    out.write_bytes(raw)
    print(f'Built {len(cases)} synthetic scenarios, {sum(len(c["checks"]) for c in cases)} probes; sha256 {digest(raw)}')


if __name__ == '__main__':
    require(sys.flags.isolated and not sys.flags.optimize, 'use python3 -I')
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'merge':
        merge(sys.argv[2], sys.argv[3])
    elif mode == 'stats':
        stats(json.loads(Path(sys.argv[2]).read_bytes()), int(sys.argv[3]))
    elif mode == 'build':
        build(json.loads(Path(sys.argv[2]).read_bytes()), int(sys.argv[3]), sys.argv[4])
    elif mode == 'replay':
        modules()
        replay(sys.argv[2], sys.argv[3], '--dev' in sys.argv)
    else:
        raise SystemExit('choose merge DIR OUT | stats MERGED SEED | build MERGED SEED OUT | replay FIX OUTDIR [--dev]')

"""C1-trim-r5: value-preserving sem-r3 level-3 trim (V1) and level-3-off (V2) vs exact r4 (python3 -I, stdlib, no model/network).

Modes (run from anywhere; all paths are relative to this file):
  stats  SEED                 counts of the generated cohort only (compressor never run)
  build  SEED OUT.json        write a fixture file (refuses to overwrite)
  replay FIXTURES OUTDIR [--dev]   score arms A/B0/B1/B2 on a fixture file. Without --dev, FREEZE.txt must match.
vendor/ holds the pinned sources (sem-r1..r3 + r4 baseline + render oracle); FREEZE.txt pins sha256 of everything.
Arms: A = exact r4 | B0 = unmodified sem-r3 (report only) | B1 = sem-r3 + V1 value-preserving trim |
      B2 = sem-r3 + V2 (levels 1-2 only). One change per variant, no after-the-fact guard in any arm.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from hashlib import sha256
import json
import random
import re
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor'
SEED = 20261129
SRC = {   # vendored, hash-pinned (verbatim copies of sources recorded in the repo history / evidence)
    'brief_compress': '5c54ae421db872d20b28434fec47ae4070756b48db9b6dda0ed4a7819688d976',
    'brief_compress_sem': '524eef440e4c87a7d1e97f322d8c411dd11e5ad1c9ddf24f3c3c25fe44b06d97',
    'brief_compress_sem2': '129641dddacf1a4aad10077bf41304f9b3c8865ac2fc928b8e560b05e9f59101',
    'brief_compress_sem3': '6383f5552c7efe19182773ac17cebcdb424d2acf3bbca8df8b9c6fec4af1a712',
    'baseline_r4': '5a88bc8b9dd27e7c455480dba4eaec11d47dba8de6add812c77bf096a7b0aa64',
    'oracle': 'd34b5cacd77107926c9e2109436eef45b93fb090ce251e46454e5e396f8ca099',
}
MIN_WARM_KEPT, MIN_EXERCISED = 12, 8

# ================================================================== V1 / V2 (the only code under test)
EDGE = '.,;:!?\'"()'
# V1 filler lexicon: generic English discourse markers / intensifiers / politeness words. Fixed BEFORE the r5 cohort
# was generated (see FREEZE.txt); a token not listed here is treated as value-bearing and is never dropped.
FILLER_WORDS = frozenset(
    'actually basically honestly really just simply please kindly well so oh okay ok um uh er hmm anyway anyways '
    'btw fyi also again still indeed obviously literally frankly seriously definitely certainly perhaps maybe '
    'quite very pretty totally now currently sorry thanks hi hello hey oops alright'.split())
FILLER_PHRASES = ('by the way', 'for the record', 'just so you know', 'so you know', 'you know', 'to be honest',
                  'heads up', 'i think', 'i guess', 'i mean', 'of course', 'as usual', 'sort of', 'kind of')
_PHRASE_RE = [re.compile(r'(?<![\w-])' + re.escape(p) + r'(?![\w-])[,;:]?[ \t]*', re.I) for p in FILLER_PHRASES]


def strip_filler(sentence):
    """(sentence without filler phrases/words, the set of tokens removed). Every other token (labels, names, places,
    colours, codewords, digit words, nouns, verbs) is kept in order."""
    s, removed = sentence, set()
    for rx in _PHRASE_RE:
        for m in rx.finditer(s):
            removed |= {t.strip(EDGE) for t in m.group(0).split()}
        s = rx.sub('', s)
    toks = s.split()
    kept = [t for t in toks if t.strip(EDGE).lower() not in FILLER_WORDS]
    removed |= {t.strip(EDGE) for t in toks if t.strip(EDGE).lower() in FILLER_WORDS}
    return ' '.join(kept), removed


def _tokens(text):
    return {t.strip(EDGE) for t in text.split() if t.strip(EDGE)}


def only_filler_dropped(orig_line, kept_line, removed=frozenset()):
    """True when every token of the asker line that is missing from the kept line is a filler word, or a token of a
    filler phrase/word that strip_filler removed (`removed`)."""
    return all(t.lower() in FILLER_WORDS or t in removed for t in _tokens(orig_line) - _tokens(kept_line))


def v1_trim_levels(sem1, bc, exchange):
    line = exchange[0]
    lab = bc._label(line) or bc._label(line, bc.SPEAKER_ANY_CASE)
    prefix = f'{lab}: ' if lab else ''
    sent = sem1.statement(exchange)
    if not sent:
        return []
    body = line[len(prefix):]
    out = []
    for level, ex in ((1, [prefix + sent] + list(exchange[1:])), (2, [prefix + sent])):
        if only_filler_dropped(body, ex[0][len(prefix):]):   # levels 1-2 may drop padding only if it is filler
            out.append((level, ex))
    stripped, removed = strip_filler(sent)
    if stripped and stripped != sent and sem1.HAS_VALUE.search(stripped) and \
            only_filler_dropped(body, stripped, removed):   # also catches a value in a sentence statement() skipped
        out.append((3, [prefix + stripped]))
    return out


def make_trim_exchange(kind, sem3, sem1, bc):
    """Replacement for sem3.trim_exchange: V1 = value-preserving levels, V2 = sem3's levels 1-2 only."""
    def trim_exchange(exchange, slot_chars, slack=sem3.SLACK):
        levels = v1_trim_levels(sem1, bc, exchange) if kind == 'V1' else \
            [x for x in sem3.trim_levels(exchange) if x[0] < 3]
        for level, ex in levels:
            if bc._size([ex]) <= slot_chars + slack:
                return level, ex
        return None, None
    return trim_exchange


# ================================================================== cohort authoring (fresh content, seed 20261129)
NOISE = [  # (noun, 7 items) one per scenario
    ('Weaving loft', 'shuttle bobbin warp heddle reed beam treadle'),
    ('Fish dock', 'crate scale net hook ice barrow ledger'),
    ('Candle bench', 'wick mould wax pitcher thermometer tray ladle'),
    ('Orchard row', 'ladder basket pruner stake twine bin hose'),
    ('Sail loft', 'needle awl batten fid thimble mallet cloth'),
    ('Brew house', 'mash paddle cask valve hop sack hydrometer'),
    ('Clock shop', 'loupe spring gear dial pin tweezer lamp'),
    ('Bindery', 'awl folder thread press glue board cloth'),
    ('Foundry floor', 'ladle tong core mould sand flask bellows'),
    ('Pottery yard', 'slab wedge rib stamp glaze trowel tile'),
    ('Harbour office', 'chart radio flag logbook pen mug kettle'),
    ('Mushroom cellar', 'tray spawn mist rack knife crate lamp'),
    ('Violin shop', 'scroll bridge peg rosin bow case plane'),
    ('Stone yard', 'wedge chisel lever sled block rule hammer'),
    ('Quilt room', 'batting hoop thimble ruler patch frame pin'),
    ('Tea store', 'caddy scoop tin sieve scale jar spoon'),
    ('Glass workshop', 'pipe blowtorch jack block shear marver pontil'),
]
OBJ = ('bike lock|laptop|passport|season ticket|guitar|camera lens|garden shed|storage unit|parking bay|gym locker|'
       'library card|bus pass|toolbox|e-reader|smart meter|canoe|van|allotment plot|piano|drone|trailer|sewing machine'
       ).split('|')
EVT = ('dental check|eye test|yoga class|piano lesson|tax review|vet visit|haircut|massage|driving test|'
       'job interview|book club|choir rehearsal|boiler service|school meeting|wine tasting|osteopath visit|'
       'language exam|pottery class').split('|')
STEMS = ('Harbour Mantel Cairn Furrow Bellows Anchor Skiff Rudder Cleat Halyard Jib Keel Lantern Glen Moor Ridge Dale '
         'Fell Tarn Heath Briar Thatch Cobble Hearth Kettle Latch Sconce Pantry Larder Cellar Parlour Attic Gable '
         'Eave Dormer').split()
NAMES = ('Cosima Idris Maren Tomas Nadia Kofi Linnea Arjun Selma Viktor Aiko Rowan Dalia Emil Ximena Jonas Keiko '
         'Abel Soraya Matteo Greta Tariq Odette Felix Ayasha Nils Marisol Cyrus Palmira Ewan').split()
COLOURS = ('scarlet azure sepia jade magenta tawny cerulean vermilion umber periwinkle fuchsia sienna khaki maroon '
           'turquoise lavender').split()
PLACES = ('Bruges Gdansk Valletta Reykjavik Salzburg Dubrovnik Zagreb Seville Bologna Riga Faro Cork Turku Graz '
          'Palermo Utrecht').split()
CODES = ('harrier pelican sequoia mistral obsidian paprika lagoon tamarind cypress monsoon kestrel nutmeg glacier '
         'badger pumice fennel').split()
DAYS = 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split()
TIMES = '7am 8am 9am 10am 11am 12pm 1pm 2pm 3pm 4pm 5pm 6pm'.split()
PAIR_KINDS = ['D1', 'D2', 'D3', 'N1', 'N2', 'N3p', 'N3c', 'N4', 'N5', 'N6', 'N7']
PAIR_WEIGHTS = [7, 7, 5, 11, 7, 8, 5, 5, 4, 7, 6]
STATIC_KINDS = ['S1', 'S2', 'S3', 'S4', 'S5']
STATIC_WEIGHTS = [4, 4, 3, 3, 3]
EVENT_KINDS = {'D2', 'N1', 'N2', 'N6', 'N7'}
N_SCEN = 17   # the last scenario is the zero-probe route/repeat control


class Pool:
    def __init__(self, rng, items):
        self.rng, self.src, self.items = rng, list(items), []

    def take(self):
        if not self.items:
            self.items = list(self.src)
            self.rng.shuffle(self.items)
        return self.items.pop()


def pair(rng, kind, thing, used, pools):
    day, (t1, t2) = rng.choice(DAYS), rng.sample(TIMES, 2)
    n1, n2 = rng.randrange(2, 9), rng.randrange(2, 9)

    def stem():
        for _ in range(200):
            v = f'{pools["stem"].take()}-{rng.randrange(3, 140)}'
            if v not in used:
                used.add(v)
                return v

    def pick(pool):
        v = pools[pool].take()
        used.add(v)
        return v
    short = rng.random() < 0.35
    if kind == 'D1':   # digit-labelled value, filler lead-in in the update
        a, b = stem(), stem()
        return (f'The reference for my {thing} is {a}.', a), (
            f'The reference for my {thing} is now {b}.' if short else
            f'Honestly, the reference for my {thing} is now {b}, please use that.', b)
    if kind == 'D2':   # leading digit label token
        a, b = stem(), stem()
        return (f'{a}: I have my {thing} down for {day} at {t1}.', a), (
            f'{b}: Quick update, my {thing} is now on {day} at {t2}.' if not short else
            f'{b}: my {thing} is now on {day} at {t2}.', b)
    if kind == 'D3':
        a, b = stem(), stem()
        return (f'My {thing} number is {a}.', a), (f'Sorry, my {thing} number changed, it is {b} now.', b)
    if kind == 'N1':   # leading Name: label, digit clause after
        a, b = pick('name'), pick('name')
        return (f'{a}: I have my {thing} down for {day} at {t1}.', a), (
            f'{b}: my {thing} is now on {day} at {t2}.' if short else
            f'{b}: Sorry, my {thing} is now on {day} at {t2}.', b)
    if kind == 'N2':   # name in a clause BEFORE the digit clause
        a, b = pick('name'), pick('name')
        return (f'I am seeing {a} for my {thing}, on {day} at {t1}.', a), (
            f'My {thing} is with {b} now, on {day} at {t2}.' if short else
            f'Change of plan, my {thing} is with {b} now, on {day} at {t2}.', b)
    if kind == 'N3p':   # place word before the digit clause
        a, b = pick('place'), pick('place')
        return (f'My {thing} is kept at {a}, bay {n1}.', a), (
            f'Moved my {thing} to {b}, bay {n2}.' if short else
            f'Just so you know, I moved my {thing} over to {b}, bay {n2}.', b)
    if kind == 'N3c':   # codeword before the digit clause
        a, b = pick('code'), pick('code')
        return (f'The word for my {thing} is {a}, valid {n1} days.', a), (
            f'New word for my {thing}: {b}, valid {n2} days.' if short else
            f'Please note the word for my {thing} is now {b}, valid {n2} days.', b)
    if kind == 'N4':   # colour before the digit clause
        a, b = pick('colour'), pick('colour')
        return (f'My {thing} is {a}, model {n1}.', a), (f'I replaced my {thing}, it is {b} now, model {n2}.', b)
    if kind == 'N5':   # no digit anywhere: not a candidate statement
        a, b = pick('colour'), pick('name')
        return (f'My {thing} is {a}.', a), (f'I handed my {thing} to {b} yesterday.', b)
    if kind == 'N6':   # place AFTER the label, digit clause last, two commas
        a, b = pick('place'), pick('place')
        return (f'{pick("name")}: my {thing} is in {a}, on {day} at {t1}.', a), (
            f'{pick("name")}: it moved to {b}, still on {day} at {t2}.', b)
    if kind == 'N7':   # name in its own non-digit sentence BEFORE the digit sentence
        a, b = pick('name'), pick('name')
        return (f'I booked my {thing} with {a}. It is on {day} at {t1}.', a), (
            f'It is with {b} now. My {thing} is on {day} at {t2}.' if short else
            f'Heads up, it is with {b} now. My {thing} is on {day} at {t2}.', b)
    raise ValueError(kind)


def static(rng, kind, thing, used, pools):
    def pick(pool):
        v = pools[pool].take()
        used.add(v)
        return v
    n = rng.randrange(2, 9)
    if kind == 'S1':
        v = f'{pools["stem"].take()}-{rng.randrange(3, 140)}'
        used.add(v)
        return f'The tag on my {thing} is {v}.', v
    if kind == 'S2':
        v = pick('name')
        return f'The person who looks after my {thing} is {v}, ring {n} times.', v
    if kind == 'S3':
        v = pick('colour')
        return f'My {thing} is painted {v}.', v
    if kind == 'S4':
        v = pick('code')
        return f'The word for my {thing} is {v}, expires in {n} days.', v
    v = pick('place')
    return f'I store my {thing} at {v}, row {n}.', v


def scenarios(seed=SEED):
    rng = random.Random(seed)
    things_obj, things_evt = Pool(rng, OBJ * 2), Pool(rng, EVT * 2)
    out = []
    for k, (noun, items) in enumerate(NOISE, 1):
        pools = {'stem': Pool(rng, STEMS), 'name': Pool(rng, NAMES), 'colour': Pool(rng, COLOURS),
                 'place': Pool(rng, PLACES), 'code': Pool(rng, CODES)}
        used, pairs, statics = set(), [], []
        n_pairs, n_static = (4, 3) if k <= 14 else ((5, 2) if k <= 16 else (3, 3))
        kinds = rng.choices(PAIR_KINDS, PAIR_WEIGHTS, k=n_pairs)
        if not any(x.startswith('N') and x != 'N5' for x in kinds):
            kinds[0] = 'N1'
        if k % 3 == 0:
            kinds[1] = 'N2'      # a place/name clause before the digit clause in a third of scenarios
        if k % 3 == 1:
            kinds[-1] = 'N3p'
        if not any(x.startswith('D') for x in kinds) and k % 2:
            kinds[-1] = 'D2'
        for kind in kinds:
            thing = (things_evt if kind in EVENT_KINDS else things_obj).take()
            pairs.append((kind,) + pair(rng, kind, thing, used, pools) + (thing,))
        for kind in rng.choices(STATIC_KINDS, STATIC_WEIGHTS, k=n_static):
            thing = things_obj.take()
            statics.append((kind, static(rng, kind, thing, used, pools), thing))
        old = [(p[0], p[1], 'pair-old') for p in pairs] + [(s[0], s[1], 'static') for s in statics]
        rng.shuffle(old)
        new_order = list(range(len(pairs)))
        rng.shuffle(new_order)
        old_facts = [{'text': t[0], 'value': t[1], 'kind': kind, 'role': role} for kind, t, role in old]
        new_facts = [{'text': pairs[i][2][0], 'value': pairs[i][2][1], 'kind': pairs[i][0], 'role': 'pair-new'}
                     for i in new_order]
        by_val = {f['value']: f for f in old_facts + new_facts}
        probes = []
        if k != N_SCEN:
            for kind, o, nw, thing in pairs:
                cat = 'UPD_SAME' if kind.startswith('D') else 'UPD_RENAMED'
                probes.append({'key': f'u{len(probes) + 1}', 'cat': cat, 'ask': f"What's my {thing} at the moment?",
                               'current': nw[1], 'superseded': [o[1]]})
            for kind, t, thing in statics:
                probes.append({'key': f's{len(probes) + 1}', 'cat': 'STATIC',
                               'ask': f'Remind me what I said about my {thing}.', 'current': t[1], 'superseded': []})
            assert all(p['current'] in by_val for p in probes)
        out.append({'name': f'r5-{k:02d}-' + noun.lower().replace(' ', '-'),
                    'noise': {'topic': noun.lower(), 'noun': noun, 'items': items.split()},
                    'old': old_facts, 'new': new_facts, 'probes': probes})
    return out


def value_class(v):
    return 'digit' if any(ch.isdigit() for ch in v) else 'non-digit'


def stats(seed):
    sc = scenarios(seed)
    probes = [p for s in sc for p in s['probes']]
    print(json.dumps({'seed': seed, 'scenarios': len(sc), 'probes': len(probes),
                      'zero_probe_scenarios': sum(not s['probes'] for s in sc),
                      'probes_by_cat': Counter(p['cat'] for p in probes),
                      'probe_current_value_class': Counter(value_class(p['current']) for p in probes),
                      'fact_kinds': Counter(f['kind'] for s in sc for f in s['old'] + s['new']),
                      'superseding_probes': sum(bool(p['superseded']) for p in probes)}, sort_keys=True, default=dict))


# ================================================================== harness
def digest(raw):
    return sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + '\n').encode()


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def load(name, source, filename):
    mod = ModuleType(name)
    mod.__file__ = str(VENDOR / filename)
    sys.modules[name] = mod
    exec(compile(source, filename, 'exec'), mod.__dict__)
    return mod


M = {}


def modules():
    for name, h in SRC.items():
        data = (VENDOR / f'{name}.py').read_bytes()
        require(digest(data) == h, f'hash mismatch: vendor/{name}.py')
    for name in ('brief_compress', 'brief_compress_sem', 'brief_compress_sem2', 'brief_compress_sem3'):
        M[name] = load(name, (VENDOR / f'{name}.py').read_text(), f'{name}.py')
    M['prior'] = load('prior_oracle', (VENDOR / 'oracle.py').read_text(), 'oracle.py')
    M['a'] = load('exact_r4', (VENDOR / 'baseline_r4.py').read_text(), 'baseline_r4.py')
    M['variants'] = {k: make_trim_exchange(k, M['brief_compress_sem3'], M['brief_compress_sem'],
                                           M['brief_compress']) for k in ('V1', 'V2')}


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
        require(prior.has(v, full) and not prior.has(v, noise_text) and v.lower() not in
                FILLER_WORDS, f'{sid}: value binding {v}')
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
                     'dropped_words': sorted(_tokens(orig) - _tokens(kept))})
    return rows


def replay(fixture_path, outdir, dev):
    prior = M['prior']
    raw = Path(fixture_path).read_bytes()
    if not dev:
        manifest = {name: h for h, name in (ln.split('  ', 1) for ln in (ROOT / 'FREEZE.txt').read_text().splitlines())}
        expect = {f'vendor/{n}.py': h for n, h in SRC.items()}
        for name in ('PREREG.md', 'c1_trim_r5.py'):
            expect[name] = digest((ROOT / name).read_bytes())
        expect['fixtures.json'] = digest(raw)
        require(manifest == expect, 'freeze mismatch')
    fixture = json.loads(raw)
    require(fixture['source'] == 'synthetic_only' and fixture['budget_chars'] == prior.BUDGET, 'fixture identity')
    arms = ('B0', 'B1', 'B2')
    vname = {'B0': None, 'B1': 'V1', 'B2': 'V2'}
    counts = {x: defaultdict(Counter) for x in arms}
    losses_report = {x: [] for x in arms}
    details, flips = {}, {x: Counter() for x in arms}
    ex_b0 = ctrl = eligible = 0
    kept = {x: 0 for x in arms}
    changed = {x: 0 for x in arms}
    keymiss_ok = {x: 0 for x in arms}
    over_budget = {x: 0 for x in arms}
    trimmed_n = {x: 0 for x in arms}
    n = len(fixture['cases'])
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
            kept[arm] += (w['output'] != base)    # stricter "keeps the warm route" (pre-stated): route did something
            trimmed_n[arm] += bool(w['info']['trimmed'])
            if arm == 'B0':
                ex_b0 += bool(w['info']['trimmed'])
            rows = trim_report(c, w['info'], vname[arm])
            details[sid]['arms'][arm] = {'trims': rows, 'untrimmed': w['info']['untrimmed'],
                                         'warm_changed_vs_r4': w['output'] != base, 'warm_chars': len(w['output']),
                                         'trim_lost_labelled': sum(len(r['lost_labelled_values']) for r in rows)}
            for mode, run in runs.items():
                out, ready = run['output'], run['key_ready_at_assembly']
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
    harness_ok = ex_b0 >= MIN_EXERCISED and ctrl == eligible
    verdicts = {}
    for arm in ('B1', 'B2'):
        if not harness_ok or sync[arm] or nflip[arm] or keymiss_ok[arm] != n:
            verdicts[arm] = 'NULL(harness)'
        elif losses_report[arm] or over_budget[arm]:
            verdicts[arm] = 'FAIL(loss)'
        elif kept[arm] < MIN_WARM_KEPT:
            verdicts[arm] = 'FAIL(degenerate)'
        else:
            verdicts[arm] = 'PASS(holdout)'
    summary = {'verdicts': verdicts, 'scenarios': n, 'probes': sum(len(c['checks']) for c in fixture['cases']),
               'non_digit_current_probes': sum(value_class(p['current']) == 'non-digit' for c in fixture['cases']
                                               for p in c['checks']),
               'a_r4_current_present_per_state': counts['B1']['warm']['r4_current_present'],
               'arms': {x: {'new_losses_warm': sum(r['mode'] == 'warm' for r in losses_report[x]),
                            'new_losses_key_miss': sum(r['mode'] == 'key-miss' for r in losses_report[x]),
                            'loss_class_counts': dict(Counter(r['class'] for r in losses_report[x])),
                            'losses': losses_report[x],
                            'warm_kept_strict': kept[x], 'warm_changed_vs_r4': changed[x],
                            'warm_kept_not_fallback': n, 'keymiss_equals_r4': keymiss_ok[x],
                            'warm_over_budget': over_budget[x], 'scenarios_with_trim': trimmed_n[x],
                            'sync_attempts_total': sync[x], 'flips': dict(flips[x]),
                            'strata': {m: dict(v) for m, v in counts[x].items()}} for x in arms},
               'controls': {'known_loss_eligible': eligible, 'known_loss_fired': ctrl,
                            'trim_branch_exercised_B0': ex_b0, 'min_exercised': MIN_EXERCISED,
                            'min_warm_kept': MIN_WARM_KEPT},
               'fixtures_sha256': digest(raw), 'dev': dev, 'model_called': False, 'network': False,
               'details': details}
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').write_bytes(encode(summary))
    print(encode({k: v for k, v in summary.items() if k != 'details'}).decode(), end='')


def build(seed, out):
    modules()
    out = Path(out)
    require(not out.exists(), 'refusing to overwrite')
    cases = [make_case('R5', i, sc) for i, sc in enumerate(scenarios(seed), 1)]
    raw = encode({'schema': 1, 'source': 'synthetic_only', 'readiness': 'observed_offline_controlled_cache',
                  'budget_chars': M['prior'].BUDGET, 'seed': seed, 'cases': cases})
    out.write_bytes(raw)
    print(f'Built {len(cases)} synthetic scenarios, {sum(len(c["checks"]) for c in cases)} probes; sha256 {digest(raw)}')


if __name__ == '__main__':
    require(sys.flags.isolated and not sys.flags.optimize, 'use python3 -I')
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'stats':
        stats(int(sys.argv[2]))
    elif mode == 'build':
        build(int(sys.argv[2]), sys.argv[3])
    elif mode == 'replay':
        modules()
        replay(sys.argv[2], sys.argv[3], '--dev' in sys.argv)
    else:
        raise SystemExit('choose stats SEED | build SEED OUT | replay FIX OUTDIR [--dev]')

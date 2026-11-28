import json
import sys
from collections import Counter
s = json.load(open(sys.argv[1]))
for arm in ('B0', 'B1'):
    a = s['arms'][arm]
    det = [d['arms'][arm] for d in s['details'].values()]
    lv = Counter(t['level'] for d in det for t in d['trims'])
    print(arm, 'losses_warm', a['new_losses_warm'], 'classes', a['loss_class_counts'],
          'kinds', dict(Counter(x['kind'] for x in a['losses'])),
          'trims', sum(len(d['trims']) for d in det), 'levels', dict(lv),
          'trim_lost_labelled', sum(d['trim_lost_labelled'] for d in det),
          'untrimmed_lines', sum(d['untrimmed'] for d in det),
          'changed', a['warm_changed_vs_r4'], 'kept_strict', a['warm_kept_strict'], 'trim_scen', a['scenarios_with_trim'],
          'scen_with_loss', len({x['id'].rsplit('/', 1)[0] for x in a['losses']}))
print('stale_added', {arm: {m: s['arms'][arm]['strata'][m]['added_stale_exposures'] for m in ('warm', 'key-miss')} for arm in s['arms']})
print('chars', {arm: sum(d['arms'][arm]['warm_chars'] for d in s['details'].values()) for arm in s['arms']})
print('B0 stale', sum(d['arms']['B0']['stale_exposed_warm'] for d in s['details'].values()))
print('kinds in B1 trims dropped words sample', Counter(w for d in s['details'].values() for t in d['arms']['B1']['trims'] for w in t['dropped_words']).most_common(12))
print(s['controls'], 'probes', s['probes'], 'nondigit', s['non_digit_current_probes'])
b = s['benefit']['per_scenario']
print('per-scenario reduction', [(r['id'][-2:], r['reduction_chars'], r['stale_a'], r['stale_b1']) for r in b])
print('range a_chars', min(r['a_chars'] for r in b), max(r['a_chars'] for r in b), 'b1', min(r['b1_chars'] for r in b), max(r['b1_chars'] for r in b))

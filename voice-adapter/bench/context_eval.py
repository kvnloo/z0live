"""Track E. Ask local Gemma about a frozen synthetic memory tape in two context layouts."""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import statistics
import sys

from eval_local import LocalModel, local_args, rate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tiers


@dataclass(frozen=True)
class Turn:
    role: str
    text: str
    update: tuple[str, str] | None = None


@dataclass(frozen=True)
class Probe:
    key: str
    ask: str
    current: str | None
    superseded: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    stable: str
    chat: tuple[Turn, ...]
    call: tuple[Turn, ...]
    probes: tuple[Probe, ...]


def exchange(turns):
    return [(t.role, t.text) for t in turns]


def noise(n, start=0):
    materials = ('paper', 'linen', 'wood', 'clay', 'glass', 'felt', 'cork')
    return tuple(Turn('user' if i % 2 == 0 else 'assistant',
                      f'Workshop note {i}: the {materials[i % len(materials)]} sample has a subtle texture. '
                      'We can compare it with the other samples after the display is assembled. '
                      'No need to make a decision about the display yet.') for i in range(start, start + n))


def plant(key, value, template):
    # Metadata describes this exact utterance; it does not come from the question/answer key.
    return Turn('user', template.format(value), (key, value))


def suite():
    days = [('Monday', 'Thursday', 'Saturday'), ('Thursday', 'Friday', 'Sunday'), ('Sunday', 'Tuesday', 'Wednesday')]
    scenarios = []
    for i, (old, middle, latest) in enumerate(days):
        studio, locker, vendor = f'Cedar-{14+i}', f'Slate-{31+i}', f'Mint-{57+i}'
        old_room, room, pickup = f'Birch-{40+i}', f'Alder-{70+i}', f'Elm-{24+i}'
        stable = f'My usual studio code is {studio}. My usual locker label is {locker}.'
        chat = (plant('Workshop vendor code', vendor, 'My workshop vendor code is {}.'),
                plant('Flight weekday', old, 'My flight is on {}.')) + noise(12) + (
                plant('Breakout room code', old_room, 'They booked breakout room {} for me.'),
                plant('Flight weekday', middle, 'Change of plan: my flight moved to {}.')) + noise(49 + 9*i, 12)
        # The snapshot contains competing values even after its chat-tail cap.
        call = (Turn('user', f'As of this morning, my flight was on {middle} and my room was {old_room}.'),
                ) + noise(8 + 9*i, 100) + (
            plant('Breakout room code', room, 'Correction: my breakout room is {}, not the earlier room.'),
            plant('Flight weekday', latest, 'Final update: my flight is now on {}.'),
        ) + noise(12, 150) + (
            Turn('user', f'I just got the parcel pickup code: {pickup}.'),  # recent verbatim, not indexed
            Turn('assistant', 'Got it. I can remind you when you head over.'),
        )
        code = ' Please give just the code.'
        probes = (Probe('studio', 'What is my usual studio code?' + code, studio),
                  Probe('locker', 'What is my usual locker label? Just the label, please.', locker),
                  Probe('vendor', 'What is my workshop vendor code?' + code, vendor),
                  Probe('flight', 'What day is my flight now? Just the weekday, please.', latest, (old, middle)),
                  Probe('room', 'What is my current breakout room code?' + code, room, (old_room,)),
                  Probe('parcel', 'What is my parcel pickup code?' + code, pickup),
                  Probe('ticket', 'What is my gallery ticket code?' + code, None),
                  Probe('wifi', 'What is my workshop Wi-Fi code?' + code, None))
        scenarios.append(Scenario(f'workshop-{i+1}', stable, chat, call, probes))
    return tuple(scenarios)


def snapshot(scenario):
    brief = scenario.stable + '\n' + '\n'.join(f'{r}: {t}' for r, t in exchange(scenario.chat))
    return tiers.snapshot(brief, exchange(scenario.call))


def blackboard(turns):
    entries = {}
    for turn in turns:
        if turn.update:
            key, value = turn.update
            entries.pop(key, None)
            entries[key] = value
    return entries


def budgeted(scenario, count, budget, last_n):
    facts = list(blackboard(scenario.chat + scenario.call).items())
    recent = exchange(scenario.call[-last_n:]) if last_n else []
    while True:
        turns = '\n'.join(f'{"User" if r == "user" else "You"}: {t}' for r, t in recent)
        board = '\n'.join(f'{key}: {value}' for key, value in facts)
        text = f'Stable brief:\n{scenario.stable}\n\nRecent call turns:\n{turns}\n\nBlackboard (current values):\n{board}'
        tokens = count(text)
        if tokens <= budget:
            return text, tokens, len(recent), len(facts)
        if recent:
            recent.pop(0)
        elif facts:
            facts.pop(0)
        else:
            raise ValueError('token budget cannot fit the stable brief and section headers')


def score(probe, reply):
    if not reply:
        return 'ERROR'
    if tiers.SKIP.search(reply):
        return 'SKIP'
    value = reply.strip().rstrip('.!').casefold()
    prefix = {
        'flight': r'(?:your |the )flight is (?:now )?on ',
        'room': r'(?:your |the )(?:current )?breakout room code is ',
        'locker': r'(?:your |the )(?:usual )?locker label is ',
        'studio': r'(?:your |the )(?:usual )?studio code is ',
        'vendor': r'(?:your |the )workshop vendor code is ',
        'parcel': r'(?:your |the )parcel pickup code is ',
        'ticket': r'(?:your |the )gallery ticket code is ',
        'wifi': r'(?:your |the )workshop wi-fi code is ',
    }.get(probe.key)
    if prefix:
        value = re.sub(r'^' + prefix, '', value)
    if probe.current and value == probe.current.casefold():
        return 'RECALL'
    if value in {old.casefold() for old in probe.superseded}:
        return 'SUPERSEDED'
    # Slot values are opaque codes or single words. Do not pretend to score free-form claims.
    if re.fullmatch(r'[a-z]+(?:-?\d+)?', value) and value not in {'unknown', 'none', 'unavailable'}:
        return 'INVENTED'
    return 'OFF_FORMAT'


def summarize(rows, contexts):
    planted = [r for r in rows if r['current'] is not None]
    corrected = [r for r in planted if r['superseded']]
    tokens = [c['context_tokens'] for c in contexts]
    median = statistics.median(tokens)
    return {'recall': rate(sum(r['result'] == 'RECALL' for r in planted), len(planted)),
            'superseded_fact_error': rate(sum(r['result'] == 'SUPERSEDED' for r in corrected), len(corrected)),
            'invented_fact': rate(sum(r['result'] == 'INVENTED' for r in rows), len(rows)),
            'absent_skip': rate(sum(r['current'] is None and r['result'] == 'SKIP' for r in rows),
                                len(rows) - len(planted)),
            'off_format': sum(r['result'] == 'OFF_FORMAT' for r in rows),
            'errors': sum(r['result'] == 'ERROR' for r in rows),
            'context_tokens': {'median': median, 'min': min(tokens), 'max': max(tokens)},
            'tab_warm_context_ttft_inferred_s': round(0.34 + 0.0017 * median, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    local_args(parser)
    parser.add_argument('--budget', type=int, default=300, help='hard context-only token cap')
    parser.add_argument('--last-n', type=int, default=6, help='upper bound on recent individual turns')
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if args.budget < 1 or args.last_n < 0 or args.repeats < 1:
        parser.error('budget and repeats must be positive; last-n must be nonnegative')
    model = LocalModel(args.port, args.model)
    arms = ('snapshot', 'three_part')
    rows, contexts = [], []
    for scenario in suite():
        full = snapshot(scenario)
        short, tokens, recent, entries = budgeted(scenario, model.count, args.budget, args.last_n)
        contexts.extend(({'scenario': scenario.name, 'arm': 'snapshot', 'context_tokens': model.count(full)},
                         {'scenario': scenario.name, 'arm': 'three_part', 'context_tokens': tokens,
                          'recent_turns': recent, 'blackboard_entries': entries}))
        systems = {arm: tiers.SYSTEM.format(tier='fast') + '\n\n' + text
                   for arm, text in zip(arms, (full, short))}
        for trial in range(args.repeats):
            for index, probe in enumerate(scenario.probes):
                for arm in arms if (index + trial) % 2 == 0 else arms[::-1]:
                    try:
                        reply, prompt_tokens = model.ask(systems[arm], probe.ask, 120)
                    except RuntimeError:
                        reply, prompt_tokens = '', None
                    rows.append({'scenario': scenario.name, 'arm': arm, 'trial': trial + 1, **vars(probe),
                                 'reply': reply, 'prompt_tokens': prompt_tokens, 'result': score(probe, reply)})
    results = {arm: summarize([r for r in rows if r['arm'] == arm], [c for c in contexts if c['arm'] == arm])
               for arm in arms}
    print(json.dumps({'harness': 'context-v1', 'model': model.info, 'budget': args.budget, 'last_n': args.last_n,
                      'repeats': args.repeats, 'results': results, 'contexts': contexts, 'rows': rows}, indent=2))
    return int(any(result['errors'] for result in results.values()))


if __name__ == '__main__':
    raise SystemExit(main())

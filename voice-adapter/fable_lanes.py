#!/usr/bin/env python3
"""Five parallel Haiku lanes, each with the Fable advisor, on one question (claude-fable-advisor skill).

usage: fable-lanes.py "question" [--out DIR] [--timeout S]
Each lane gets the same question plus its own angle, runs read-only, and must call the advisor once
before answering (the main model skips consults unless the process is mandatory). Answers land in
DIR/<lane>.json as each finishes; DIR/summary.txt collects them. Prints one line per lane as it lands.
"""
import argparse, json, os, re, subprocess, sys, threading, time
from pathlib import Path

# Host-portable roots: this file lives in <z0live>/voice-adapter; the home repo sits beside the other repos.
Z0LIVE = os.environ.get('Z0LIVE_REPO', str(Path(__file__).resolve().parents[1]))
HOME_REPO = os.environ.get('Z0LIVE_HOME_REPO') or next(
    (p for p in (os.path.expanduser('~/workspace/repos/home'), '/workspace/repos/home') if os.path.isdir(p)),
    '/workspace/repos/home')
ANGLES = {
    'service': 'Service health: `systemctl --user status z0live.service`, ports 8791 (broker) and 8644 (Hermes API) with `ss -ltnp`, recent errors in `journalctl --user -u z0live.service -n 200 --no-pager`.',
    'session': f'The live call: read-only sqlite on the broker db under $Z0LIVE_DATA (see {Z0LIVE}/desktop and `systemctl --user cat z0live.service` for the data dir). Active sessions, last events, wedged/degenerate calls, inbox items stuck.',
    'transport': f'Voice transport and model lanes: {Z0LIVE}/docs (LIVE-BIDI.md, VOICE-TOOLS.md, API.md), service.env bindings (Z0LIVE_DELEGATE_MODEL etc.), bidi vs realtime errors in the journal.',
    'relay': f'Relay/delegation into Hermes: {Z0LIVE}/docs/RELAY.md, relay.main_session target, ~/.hermes/profiles/clean/logs/errors.log lines for session.resume / gateway_refused / admission_conflict in the last hour.',
    'history': f'What changed recently: `git -C {Z0LIVE} log --oneline -15` and current HEAD/branch, plus known open issues `gh issue list -R kvnloo/z0live --limit 15`. Match the symptom to a known bug.',
}
Z0LIVE_ANGLES = dict(ANGLES)
# Default lanes answer whatever Kevin asked; the z0live-debug angles above apply only when he asks about
# the call/voice stack itself (a 15:18 PR question got five z0live health reports otherwise).
GENERAL = {
    'sessions': 'Live Hermes sessions (the Hermes backend; what every bot/tab is doing now): for each '
                '~/.hermes/profiles/*/state.db run `sqlite3 -readonly DB "select profile_name,id,title,'
                "datetime(last_activity_at,'unixepoch','localtime'),last_activity_description from sessions "
                "where ended_at is null and last_activity_at > strftime('%s','now')-3*3600 "
                'order by last_activity_at desc limit 15"`; then the latest user message of the top few '
                '(messages where session_id=ID and role=\'user\' order by timestamp desc limit 1). Report title, age, status.',
    'github': 'GitHub, as kvnloo: `gh search prs --author kvnloo --sort updated --limit 30` (add --merged/--state), '
              '`gh pr view URL --comments`, `gh api repos/OWNER/REPO/pulls/N/reviews`, `gh api` for who merged/reviewed.',
    'history': 'Kevin\'s past agent sessions: read-only `sqlite3 -readonly` on each ~/.hermes/profiles/*/state.db '
               '(messages_fts MATCH keywords; messages has session_id, role, content, timestamp). Newest first. '
               "Add `and coalesce(m.content,'') != ''`: empty-content rows are earlier agents' search calls, not history.",
    'files': f'Local notes and repos: {HOME_REPO}/map/INDEX.md names every repo; Grep/Read the matching ones '
             'and /mnt/zer0models/workspace/zer0/data/operations for receipts and ledgers.',
    'answer': 'Answer directly from what you know plus at most one quick read; say what you would need to check.',
    'web': 'Public web: WebSearch/WebFetch for upstream projects, docs or people mentioned.',
}
LIVE_WORDS = re.compile(r'\b(z0live|this call|voice|lanes?|relay|latency|glitch\w*|broker|transport)\b', re.I)
GENERAL_INTRO = ('You are a FAST LANE helping Kevin during his live voice call: answer exactly what he asked, '
                 'not something adjacent. READ ONLY: never edit, post, comment, merge or restart anything. '
                 'Final reply: <=4 short speech-friendly lines with concrete names/numbers/links, '
                 'or "nothing found on this angle".')
CWD, MODEL = HOME_REPO, 'haiku'
TOOLS = ['Read', 'Grep', 'Glob', 'WebSearch', 'WebFetch', 'Bash(journalctl:*)', 'Bash(systemctl --user status:*)',
         'Bash(systemctl --user cat:*)', 'Bash(ss:*)', 'Bash(git -C:*)', 'Bash(gh search:*)', 'Bash(gh pr:*)',
         'Bash(gh issue list:*)', 'Bash(gh api:*)', 'Bash(sqlite3 -readonly:*)', 'Bash(curl -s:*)', 'Bash(tail:*)', 'Bash(ls:*)']

# A `claude` lane child gets exactly these variables and nothing else from the service env (provider keys,
# tokens and the rest of voice.env stay in the adapter).
CHILD_ENV_KEYS = ('PATH', 'HOME', 'CLAUDE_CONFIG_DIR', 'FAST_LANE_CHILD')


def claude_config_dir(environ=None):
    """Setting: Z0LIVE_CLAUDE_CONFIG_DIR in the adapter's service env (~/.config/z0live/voice.env), so lanes can
    use a separate Claude login; unset = Claude Code's default ~/.claude."""
    env = os.environ if environ is None else environ
    return os.path.expanduser(env.get('Z0LIVE_CLAUDE_CONFIG_DIR') or '~/.claude')


def claude_child_env(environ=None):
    """The complete environment for a `claude` child (CHILD_ENV_KEYS only)."""
    env = os.environ if environ is None else environ
    return {'PATH': env.get('PATH') or os.defpath, 'HOME': env.get('HOME') or str(Path.home()),
            'CLAUDE_CONFIG_DIR': claude_config_dir(env), 'FAST_LANE_CHILD': '1'}


def pick_angles(q):
    """z0live-debug angles when the question is about the live stack, else the general ones."""
    return Z0LIVE_ANGLES if LIVE_WORDS.search(q) else GENERAL


INTRO = None  # --angles FILE may replace the z0live intro, angles, cwd and tools for another domain


FAST_STEPS = 'Process: run 1-3 quick reads for your angle, then answer. Do not consult the advisor (it adds ~40 s).\n'


def prompt(q, angle, fable=True):
    if not fable:
        return (f'You are a FAST LANE on z0live (repo {Z0LIVE}) during Kevin\'s live voice call. READ ONLY.\n{FAST_STEPS}'
                f'Your angle: {angle}\n\nKevin said: {q}\n\nFinal reply: <=3 short speech-friendly lines with evidence, '
                'or "nothing found on this angle".') if not INTRO else f'{INTRO}\n{FAST_STEPS}Your angle: {angle}\n\nQuestion: {q}'
    if INTRO:
        return (f'{INTRO}\nMANDATORY process: 1) run quick reads for your angle; 2) call the advisor with what you found; '
                f'3) answer. Do not finish without the advisor call.\nYour angle: {angle}\n\nQuestion: {q}')
    return (f'You are a FAST LANE troubleshooting z0live (Kevin\'s realtime voice broker, repo {Z0LIVE}) while he is '
            'on a live voice call. Be quick. READ ONLY: never edit, restart or write anything.\n'
            'MANDATORY process: 1) run 2-5 quick reads for your angle; 2) call the advisor with what you found; '
            '3) answer. Do not finish without the advisor call.\n'
            f'Your angle: {angle}\n\nKevin said: {q}\n\n'
            'Final reply: <=5 short lines, plain speech-friendly facts with file/command evidence, or "nothing found on this angle".')


def run(name, q, out, timeout, done, fable=True):
    t0 = time.time()
    env = claude_child_env()
    cmd = ['claude', '-p', '--model', MODEL, *(['--advisor', 'fable'] if fable else []), '--output-format', 'json',
           '--allowedTools', *TOOLS]
    try:
        p = subprocess.run(cmd, input=prompt(q, ANGLES[name], fable), capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=CWD)
        d = json.loads(p.stdout or '{}')
        rec = {'lane': name, 'secs': round(time.time() - t0, 1), 'answer': (d.get('result') or p.stderr[-400:]).strip(),
               'advisor': 'claude-fable-5-2' in (d.get('modelUsage') or {}), 'session_id': d.get('session_id')}
    except Exception as e:
        rec = {'lane': name, 'secs': round(time.time() - t0, 1), 'answer': f'lane failed: {e}', 'advisor': False}
    (out / f'{name}.json').write_text(json.dumps(rec, indent=1))
    print(f"[{rec['secs']}s {name} advisor={rec['advisor']}] {' '.join(rec['answer'].split())[:600]}", flush=True)
    done.append(rec)


def main():
    a = argparse.ArgumentParser()
    a.add_argument('question'); a.add_argument('--out'); a.add_argument('--timeout', type=int, default=180)
    a.add_argument('--lanes'); a.add_argument('--angles', help='JSON {intro, angles:{name:text}, cwd?, tools?}')
    a.add_argument('--model', default='haiku')
    a.add_argument('--n', type=int, default=0, help='run only the first N angles (0 = all)')
    a.add_argument('--fable', type=int, default=1, help='how many lanes consult Fable (slow, ~40 s); the rest answer directly')
    o = a.parse_args()
    global INTRO, CWD, MODEL
    MODEL = o.model
    if o.angles:
        spec = json.load(open(o.angles))
        INTRO, CWD = spec['intro'], spec.get('cwd', CWD)
        ANGLES.clear(); ANGLES.update(spec['angles'])
        if spec.get('tools'): TOOLS[:] = spec['tools']
    elif pick_angles(o.question) is Z0LIVE_ANGLES:
        CWD = Z0LIVE
    else:
        INTRO = GENERAL_INTRO
        ANGLES.clear(); ANGLES.update(GENERAL)
    o.lanes = o.lanes or ','.join(list(ANGLES)[:o.n or None])
    out = Path(o.out or os.path.expanduser(f'~/.cache/z0live/fable-lanes/{time.strftime("%H%M%S")}'))
    out.mkdir(parents=True, exist_ok=True)
    print(f'out={out}', flush=True)
    done = []
    names = [n for n in o.lanes.split(',') if n in ANGLES]
    ts = [threading.Thread(target=run, args=(n, o.question, out, o.timeout, done, i >= len(names) - o.fable))
          for i, n in enumerate(names)]
    [t.start() for t in ts]; [t.join() for t in ts]
    (out / 'summary.txt').write_text('\n\n'.join(f"## {r['lane']} ({r['secs']}s, advisor={r['advisor']})\n{r['answer']}" for r in done))


if __name__ == '__main__':
    main()

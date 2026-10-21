"""Regression evidence from the owner's October 21 voice calls; no live services."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store, builtin_tools, reserved_tool_names
from z0live_desktop import relay, relay_status as rs, phone_control as pc
from z0live_desktop.delegation import Delegator
from test_relay import make_home, FakeGateway

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/relay-status-20261021.json').read_text())


class RelayStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = make_home(base)
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute('ALTER TABLE messages ADD COLUMN timestamp REAL DEFAULT 0')
        self.store = Store(base / 'broker')
        self.device = self.store.authenticate(self.store.pair('unit'))
        with patch('z0live_desktop.voice_runtime.load_plan', return_value=None), patch.dict(os.environ, {'Z0LIVE_LIVE_TRANSPORT': 'bidi'}):
            self.sid = self.store.new_session(self.device, {'client': 'lawnchair', 'transports': ['bidi'], 'tools': [
                {'type': 'function', 'name': name, 'parameters': {'type': 'object', 'properties': {}}}
                for name in ['phone_do', 'phone_act']]})['session_id']
        env = patch.dict(os.environ, {'Z0LIVE_RELAY_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.delegator = Delegator(self.store, hermes=object())

    def record(self, text='label gestures with my voice', ok=True):
        gateway = FakeGateway({'ok': ok, **({} if ok else {'error': 'gateway_unavailable'})})
        result = relay.relay(text, store=self.store, sid=self.sid, runner=gateway)
        self.assertEqual(result['ok'], ok)
        return result

    def observe(self, **kw):
        return rs.status(self.store.path, device=self.device, **kw)

    def user(self, text, ts=None):
        self.store.add_events(self.device, [{'session_id': self.sid, 'type': 'user_transcript',
                                             'text': text, 'ts': ts or time.time()}])

    def test_catalog_and_builtin_shape_and_validation(self):
        self.assertIn('relay_status', [t['name'] for t in builtin_tools()])
        self.assertIn('relay_status', reserved_tool_names())
        self.record()
        out = self.store.tool(self.device, 'relay_status', {'session_id': self.sid, 'arguments': {'limit': 1}})
        item = out['relays'][0]
        self.assertEqual(item['text'], 'label gestures with my voice')
        self.assertEqual(item['target_session_id'], 'local-main')
        self.assertEqual(item['current_title'], 'Debug Hermes crashing')
        self.assertEqual(item['delivery_state'], 'accepted by gateway')
        self.assertFalse(item['replied_since'])
        self.assertIsNone(item['reply_excerpt'])
        for limit in [0, 21, True, '2', 1.5]:
            with self.assertRaises(ValueError):
                self.observe(limit=limit)
        with self.assertRaises(ValueError):
            self.store.tool(self.device, 'relay_status', {'session_id': self.sid, 'arguments': {'bogus': 1}})
        with self.assertRaises(LookupError):
            self.store.tool('other-device', 'relay_status', {'session_id': self.sid})
        self.assertEqual(rs.status(self.store.path, device='other-device')['relays'], [])

    def test_transcript_delivery_current_title_topic_and_reply(self):
        text = '100% exact_under_score text'
        self.record(text)
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute("UPDATE sessions SET title='Current title' WHERE id='local-main'")
            db.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                       ('local-main', 'user', relay.compose(text), time.time() + 1))
            db.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                       ('local-main', 'assistant', 'I am updating the recorder.', time.time() + 2))
        item = self.observe()['relays'][0]
        self.assertEqual(item['delivery_state'], 'delivered')
        self.assertEqual(item['current_title'], 'Current title')
        self.assertEqual(item['latest_topic'], 'fix the ring double tap please')
        self.assertTrue(item['replied_since'])
        self.assertEqual(item['reply_excerpt'], 'I am updating the recorder.')

    def test_failures_missing_db_and_unrelated_assistant_are_honest(self):
        self.record(ok=False)
        item = self.observe()['relays'][0]
        self.assertEqual(item['delivery_state'], 'failed')
        self.assertEqual(item['error'], 'gateway_unavailable')
        with patch.dict(os.environ, {'Z0LIVE_RELAY_HOME': str(self.home / 'missing')}):
            out = self.observe()
        self.assertEqual(out['gaps'], ['target_state_unavailable'])
        self.assertIsNone(out['relays'][0]['replied_since'])

    def test_legacy_readback_is_not_a_receipt_but_exact_transcript_is(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO live_delegations(id,sid,call_id,text,route,status,created,updated,final) VALUES(?,?,?,?,?,?,?,?,?)',
                       ('d', self.sid, 'call', 'pass it along', 'hermes', 'completed', time.time(), time.time(),
                        'Sent to Debug Hermes crashing: "real payload"'))
        item = self.observe()['relays'][0]
        self.assertEqual(item['delivery_state'], 'unknown')
        self.assertIsNone(item['target_session_id'])
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                       ('local-main', 'tool', relay.compose('real payload'), time.time() + 1))
        item = self.observe()['relays'][0]
        self.assertEqual(item['target_session_id'], 'local-main')
        self.assertEqual(item['delivery_state'], 'delivered')

    def test_recorded_target_receipts_contradict_the_crash_claim(self):
        receipts = json.loads((Path(__file__).parent / 'fixtures/relay-target-receipts-20261021.json').read_text())
        target = receipts[0]['target']['session_id']
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute("UPDATE sessions SET id=? WHERE id='local-main'", (target,))
            db.execute("UPDATE messages SET session_id=? WHERE session_id='local-main'", (target,))
            for record in receipts:
                msg = record['target']
                db.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                           (target, msg['role'], msg['content'], msg['timestamp']))
            msg = receipts[0]['first_reply']
            db.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                       (target, 'assistant', msg['content'], msg['timestamp']))
        with self.store.connect() as db:
            for record in receipts:
                db.execute('INSERT INTO live_delegations(id,sid,call_id,text,route,status,created,updated,final) VALUES(?,?,?,?,?,?,?,?,?)',
                           (record['delegation_id'], self.sid, 'call', 'relay', 'hermes', 'completed',
                            record['created'], record['created'], record['final']))
        out = self.observe()
        self.assertEqual(len(out['relays']), 2)
        for item in out['relays']:
            self.assertEqual(item['target_session_id'], target)
            self.assertEqual(item['current_title'], 'Debug Hermes crashing')
            self.assertEqual(item['delivery_state'], 'delivered')
            self.assertTrue(item['replied_since'])
            self.assertIn("you'll drive it yourself through z0live", item['reply_excerpt'])

    def test_quoted_claims_do_not_prove_delivery_or_previous_reply(self):
        self.record('literal 100%_text')
        with sqlite3.connect(self.home / 'state.db') as db:
            for role, content in [
                ('assistant', relay.compose('literal 100%_text')),
                ('user', "[CONTEXT COMPACTION] " + relay.compose('literal 100%_text')),
                ('tool', relay.compose('literal 100XYZtext')),
            ]:
                db.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                           ('local-main', role, content, time.time() - 1))
        item = self.observe()['relays'][0]
        self.assertEqual(item['delivery_state'], 'accepted by gateway')
        self.assertFalse(item['replied_since'])

    def test_ambiguous_gateway_error_is_not_reported_as_not_sent(self):
        with patch('z0live_desktop.relay.deliver', side_effect=TimeoutError()):
            result = relay.relay('hello', store=self.store, sid=self.sid)
        self.assertNotIn('nothing was sent', result['say'])
        self.assertEqual(self.observe()['relays'][0]['delivery_state'], 'unknown')

    def test_real_status_transcripts_never_dispatch_phone_or_model(self):
        for row in FIXTURE:
            if row['type'] != 'user_transcript':
                continue
            self.assertFalse(pc.phone_intent(row['text']), row['text'])
            self.user(row['text'])
            for tool in ['phone_do', 'phone_act']:
                with self.assertLogs('z0live.phone_control', level='WARNING') as logs:
                    result = self.delegator.phone_call(self.sid, 'd', tool, {'goal': 'tap Phone'})
                self.assertEqual(result['result']['refused'], 'not_a_phone_request')
                self.assertIn('not_a_phone_request', logs.output[0])
                with self.assertRaisesRegex(ValueError, 'not_a_phone_request'):
                    self.store.publish('', tool, {'goal': 'tap Phone'}, self.sid)
        status_turns = [r for r in FIXTURE if r['type'] == 'user_transcript' and
                        r['session_id'] in {'c9ffaba8b6e14b99a3f71c8c5fcadda8', 'b1607f1e12e84edf9ce0eb9a7c6df92d'}]
        for row in status_turns:
            self.assertTrue(rs.wants_status(row['text']), row['text'])
            appended = []
            call = type('Call', (), {'sid': self.sid, 'closed': threading.Event(),
                                    'append': lambda _, did, text, channel=None: appended.append(text) or True})()
            with patch.object(self.delegator, 'work', side_effect=AssertionError('no LLM')), patch.object(self.delegator, 'phone', side_effect=AssertionError('no phone')):
                self.delegator.handle(call, 'not-persisted', row['text'])
            self.assertEqual(len(appended), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox WHERE action IS NOT NULL').fetchone()[0], 0)

    def test_cli_returns_structured_refusal_without_queuing_an_action(self):
        self.user('Did my message go through?')
        done = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / 'bin/z0live'),
                               'action', '--session', self.sid, '--action', 'phone_do',
                               '--params', '{"goal":"tap Phone"}'], capture_output=True, text=True,
                              env={**os.environ, 'Z0LIVE_DATA': str(self.store.directory)}, timeout=10)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'ok': False, 'refused': 'not_a_phone_request'})
        self.assertIn('not_a_phone_request', done.stderr)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)

    def test_explicit_commands_and_confirmation_stale_or_missing_user(self):
        self.assertFalse(pc.authorize(self.store, self.sid, 'phone_do', {}))
        for text in ['tap the menu button', 'Can you click on the menu button', 'open the sidebar',
                     'scroll the screen down', 'type hello in the text field', 'go to Settings']:
            self.assertTrue(pc.phone_intent(text), text)
            self.user(text)
            self.assertTrue(pc.authorize(self.store, self.sid, 'phone_do', {}))
            self.assertIsNotNone(self.store.publish('', 'phone_do', {'goal': text}, self.sid))
        self.user('press send')
        self.user('yes')
        self.assertTrue(pc.authorize(self.store, self.sid, 'phone_act', {'confirm': True}))
        self.assertFalse(pc.authorize(self.store, self.sid, 'phone_act', {}))
        self.user('tap the menu button', time.time() - 121)
        self.assertFalse(pc.authorize(self.store, self.sid, 'phone_do', {}))
        for text in ['did my message go through', 'did you send it', 'what did Hermes say',
                     'do not tap the Phone button', 'the phone has a menu button',
                     'Tell Hermes to tap the menu', 'why did you open the phone',
                     'open the pull request about the phone', 'tap my shoulder']:
            self.assertFalse(pc.phone_intent(text), text)


if __name__ == '__main__':
    unittest.main()

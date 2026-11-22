"""Recording contract tests: isolated Store/HTTP, no real device/provider/service."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import threading
import urllib.request
from types import SimpleNamespace
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store, builtin_tools
from z0live_desktop.ring_recording import voice_command


class RingRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.tokens = [self.store.pair(n) for n in ('phone','ring','other')]
        self.phone, self.source, self.other = [self.store.authenticate(t) for t in self.tokens]
        self.sid = str(uuid.uuid4())
        self.now = time.time()
        self.store.ring_recordings.clock = lambda: self.now
        self.store.gestures.clock = lambda: self.now
        with self.store.connect() as db:
            db.execute("INSERT INTO sessions(id,device,client,created,updated,context,tools,ended) VALUES(?,?,'lawnchair',?,?,'{}','[]',0)",
                       (self.sid,self.phone,self.now,self.now))
        self.store.gestures.listener(self.source, dict(source_device=self.source,enabled=True,ttl_seconds=300))

    def call(self, name, **args):
        return self.store.tool(self.phone,name,dict(session_id=self.sid,arguments=args))

    def start(self, **args):
        return self.call('ring_recording_start',kind='labelled',**args)

    def test_voice_labels_undo_stop_and_confirmation_are_not_call_actions(self):
        start = self.start(note='walking')
        self.assertEqual(start['status'],'queued')
        self.assertNotEqual(start['say'],'Recording now')
        self.now += 2
        self.call('ring_label',label='double')
        self.now += 2
        self.call('ring_label',label='triple')
        self.call('ring_label',label='undo')
        poll = self.store.ring_recordings.poll(self.source)
        self.assertEqual([v['label'] for v in poll['recording']['labels']],['none','double'])
        self.assertEqual(poll['recording']['labels'][1]['ts'],self.now-2)
        self.assertIsNone(self.store.ring_recordings.poll(self.other)['recording'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT ended FROM sessions WHERE id=?',(self.sid,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM gesture_commands').fetchone()[0],0)
        self.call('ring_recording_stop')
        self.assertEqual(self.store.ring_recordings.poll(self.source)['recording']['desired'],'stop')
        self.assertTrue(self.store.ring_recordings.suppresses(self.source))
        self.store.ring_recordings.report(self.source,dict(id=start['id'],status='saved',frames=80,counts=dict(double=4,triple=1,quadruple=2)))
        self.assertFalse(self.store.ring_recordings.suppresses(self.source))
        self.assertEqual(self.call('ring_recording_status')['device']['counts']['double'],4)

    def test_suppression_blocks_both_gestures_even_before_device_ack_and_expires(self):
        self.start()
        for gesture in ('double_tap','triple_tap'):
            result = self.store.gestures.submit(self.source,dict(source_device=self.source,gesture=gesture,ts=self.now,nonce=str(uuid.uuid4())))
            self.assertEqual(result['status'],'recording_suppressed')
        self.now += 600
        self.assertFalse(self.store.ring_recordings.suppresses(self.source))
        self.assertIsNone(self.store.ring_recordings.poll(self.source)['recording'])

    def test_device_report_is_source_scoped_and_counts_not_recall(self):
        start = self.start()
        body = dict(id=start['id'],status='recording',frames=100,counts=dict(double=3,triple=2,quadruple=0))
        self.assertFalse(self.store.ring_recordings.report(self.other,body)['ok'])
        self.assertTrue(self.store.ring_recordings.report(self.source,body)['ok'])
        result = self.call('ring_recording_status')
        self.assertNotIn('recall',result)
        self.assertEqual(result['device']['frames'],100)
        with self.assertRaises(ValueError): self.store.ring_recordings.report(self.source,{**body,'frames':True})

    def test_validation_and_ownership(self):
        with self.assertRaises(ValueError): self.call('ring_recording_start',kind='bad')
        with self.assertRaises(ValueError): self.call('ring_recording_start',kind='baseline',unexpected=True)
        with self.assertRaises(LookupError): self.store.tool(self.other,'ring_recording_start',dict(session_id=self.sid,arguments=dict(kind='baseline')))
        self.start()
        with self.assertRaises(ValueError): self.start()
        with self.assertRaises(ValueError): self.call('ring_label',label='quad')
        with self.store.connect() as db: db.execute('UPDATE sessions SET ended=1 WHERE id=?',(self.sid,))
        with self.assertRaises(ValueError): self.call('ring_label',label='double')

    def test_realtime_catalog_and_bidi_narrow_phrase_route(self):
        names = {t['name'] for t in builtin_tools()}
        self.assertTrue({'ring_recording_start','ring_label','ring_recording_stop','ring_recording_status'} <= names)
        self.assertEqual(voice_command('Recording now.'),('ring_recording_start',{'kind':'labelled'}))
        self.assertIsNone(voice_command('double'))
        self.assertEqual(voice_command('doing triples',True)[1]['label'],'triple')
        self.assertIsNone(voice_command('double the timeout',True))
        self.assertEqual(voice_command('still now',True)[1],{'label':'none','note':'still now'})
        self.assertEqual(voice_command('stop recording',True)[0],'ring_recording_stop')
        self.assertEqual(voice_command('recording status',False)[0],'ring_recording_status')
        self.assertEqual(voice_command('stop recording',False)[0],'ring_recording_stop')

    def test_http_device_roundtrip_and_retry_receipts(self):
        from z0live_desktop.server import Server, Handler
        server = Server(('127.0.0.1', 0), Handler)
        server.store, server.origins = self.store, set()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(path, body=None, token=None):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path, data=data,
                    headers={'Authorization':'Bearer '+(token or self.tokens[0]),'Content-Type':'application/json'})
                with urllib.request.urlopen(req, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    return json.load(response)
            start_body = dict(session_id=self.sid,call_id='start-1',arguments=dict(kind='labelled'))
            start = request('/v1/tools/ring_recording_start', start_body)
            self.assertEqual(request('/v1/tools/ring_recording_start', start_body),start)
            poll = request('/v1/gesture/commands',token=self.tokens[1])
            self.assertEqual(poll['recording']['id'],start['id'])
            self.assertIsInstance(poll['broker_ts'],float)
            request('/v1/gesture/recording-report',dict(id=start['id'],status='recording',frames=77,
                    counts=dict(double=3,triple=2,quadruple=1)),token=self.tokens[1])
            status = request('/v1/tools/ring_recording_status',dict(session_id=self.sid,arguments={}))
            self.assertEqual(status['device']['frames'],77)
            for label in ('double','triple'):
                self.call('ring_label',label=label)
            undo = dict(session_id=self.sid,call_id='undo-1',arguments=dict(label='undo'))
            request('/v1/tools/ring_label',undo)
            request('/v1/tools/ring_label',undo)
            labels = request('/v1/gesture/commands',token=self.tokens[1])['recording']['labels']
            self.assertEqual([v['label'] for v in labels],['none','double'])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_bidi_delegation_routes_recording_without_desktop_work(self):
        from z0live_desktop.delegation import Delegator
        class NoWork:
            def route(self, *args): raise AssertionError('ring command must not use phone router')
            def start(self, *args, **kwargs): raise AssertionError('ring command must not use Hermes')
        appended = []
        call = SimpleNamespace(sid=self.sid,call_id='fixture-call',closed=threading.Event(),
            append=lambda *args: appended.append(args) or True)
        delegator = Delegator(self.store,hermes=NoWork(),router=NoWork())
        for n, phrase in enumerate(('start ring recording','double','that was a miss','stop recording','recording status')):
            did = f'ring-test-{n}'
            if phrase == 'recording status':
                recording = self.store.ring_recordings.current(self.sid)
                self.store.ring_recordings.report(self.source,dict(id=recording['id'],status='saved',frames=100,
                    counts=dict(double=4,triple=2,quadruple=0)))
            delegator.delegate(call,did,phrase)
            end = time.time()+3
            rows = []
            while time.time() < end:
                rows = [r for r in delegator.trace(self.sid) if r['id'] == did]
                if rows and rows[0]['status'] == 'completed': break
                time.sleep(.01)
            self.assertEqual(rows[0]['status'],'completed')
            self.assertTrue(rows[0]['route'].startswith('ring:'))
        payload = json.loads(self.store.ring_recordings.current(self.sid)['payload'])
        self.assertEqual(payload['labels'][-1]['label'],'double')
        self.assertIn('miss',payload['labels'][-1]['note'])
        self.assertEqual(len(appended),5)
        self.assertIn('Saved.',appended[-1][1])


if __name__ == '__main__': unittest.main()


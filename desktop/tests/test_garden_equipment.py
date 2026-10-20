"""Garden OS equipment context is a one-time operating note for the persistent Hermes session.

No provider calls: the native API is faked. Proves the note is queued once (idempotent across
broker restarts), names only argv CLI commands, and keeps "queued" distinct from "done".
"""

from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop import garden as worker_module  # noqa: E402
from z0live_desktop.delegation import backend_instructions  # noqa: E402
from z0live_desktop.worker import Worker  # noqa: E402


class FakeWorker(Worker):
    def api(self, path, body=None, idem=None):
        return {"session": {"id": "z0live", "title": "z0live"}}


class GardenEquipmentContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def contexts(self):
        with self.store.connect() as db:
            return [r[0] for r in db.execute("SELECT text FROM jobs WHERE kind='context' ORDER BY id")]

    def test_queued_once_across_restarts(self):
        FakeWorker(self.store, threading.Event()).setup()
        FakeWorker(self.store, threading.Event()).setup()
        notes = [c for c in self.contexts() if "Garden OS (garden_room) equipment" in c]
        self.assertEqual(len(notes), 1)

    def test_existing_broker_gets_note_without_reseeding(self):
        with self.store.connect() as db:
            for name in ("initialized", "work_tools_v1"):
                db.execute("INSERT INTO meta VALUES(?, 'true')", (name,))
        FakeWorker(self.store, threading.Event()).setup()
        self.assertEqual(len(self.contexts()), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='seed'").fetchone()[0], 0)

    def test_note_contract(self):
        note = worker_module.GARDEN_EQUIPMENT
        self.assertIn(str(worker_module.GARDEN), note)
        for cmd in ("tools/equipment.py add URL", "tools/equipment.py share URL", "compare ID ID", "ideas --ids"):
            self.assertIn(cmd, note)
        self.assertIn("say queued, not done", note)
        self.assertIn("never to verify (verify with list)", note)
        self.assertIn("Never log in to Amazon", note)

    def test_delegated_ask_z0_runs_carry_the_brief(self):
        text = backend_instructions("sess-1", [{"name": "open_app"}])
        self.assertIn("tools/equipment.py add URL", text)
        self.assertIn("Voice broker session: sess-1", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

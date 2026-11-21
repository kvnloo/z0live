import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import handoff


class Decide(unittest.TestCase):
    def test_leaving_to_another_app_switches(self):
        self.assertTrue(handoff.decide("client_closed", "com.android.chrome", "auto"))
        self.assertTrue(handoff.decide("transport_closed", "app.lawnchair.play.debug", "auto"))

    def test_hanging_up_inside_hermes_switches(self):
        # the app has no back button: ending the call in-app is how Kevin leaves it (11-16)
        self.assertTrue(handoff.decide("client_closed", handoff.HERMES_APP, "auto"))
        self.assertTrue(handoff.decide("client_closed", None, "auto"))

    def test_opt_out_and_provider_close(self):
        self.assertFalse(handoff.decide("client_closed", "com.android.chrome", "off"))
        self.assertFalse(handoff.decide("provider_closed", "com.android.chrome", "always"))
        self.assertTrue(handoff.decide("client_closed", handoff.HERMES_APP, "always"))

    def test_mode_defaults_to_auto(self):
        with mock.patch.dict(os.environ, {"HERMES_VOICE_HANDOFF": ""}):
            self.assertEqual(handoff.mode(), "auto")
        with mock.patch.dict(os.environ, {"HERMES_VOICE_HANDOFF": "OFF"}):
            self.assertEqual(handoff.mode(), "off")


class Phone(unittest.TestCase):
    def test_foreground_parses_resumed_activity(self):
        dump = "  topResumedActivity=ActivityRecord{1a2b u0 com.android.chrome/.Main t42}\n"
        with mock.patch.object(handoff, "adb", return_value=dump):
            self.assertEqual(handoff.foreground("x"), "com.android.chrome")

    def test_record_keeps_last_exchanges(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "handoff.json"
            ex = [("user", f"q{i}") for i in range(12)]
            handoff.record("chat1", "clean", ex, "call1", path)
            data = json.loads(path.read_text())
            self.assertEqual(data["chat"], "chat1")
            self.assertEqual(len(data["exchanges"]), 8)
            self.assertEqual(data["exchanges"][-1], ["user", "q11"])


if __name__ == "__main__":
    unittest.main()

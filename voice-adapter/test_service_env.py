import unittest
from unittest.mock import patch

import mobile_live


class ServiceEnvTests(unittest.TestCase):
    def test_groq_key_never_looks_up_profile_files(self):
        with patch("pathlib.Path.home", side_effect=AssertionError("unexpected home lookup")), \
             patch("pathlib.Path.read_text", side_effect=AssertionError("unexpected file read")), \
             patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mobile_live._ttft_key(), "")
            with patch.dict("os.environ", {"GROQ_API_KEY": "  service-key  "}):
                self.assertEqual(mobile_live._ttft_key(), "service-key")

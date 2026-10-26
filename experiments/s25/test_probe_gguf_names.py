"""Probe the bounded GGUFv3 name guard using synthetic metadata only."""
from pathlib import Path
import struct
import tempfile
import unittest

import probe_gguf_names as m


def gguf(names):
    b=bytearray(b"GGUF" + struct.pack("<IQQ", 3, len(names), 0))
    for name in names:
        b += struct.pack("<Q", len(name)) + name
        b += struct.pack("<I", 1) + struct.pack("<Q", 8)
        b += struct.pack("<IQ", 0, 0)
    return bytes(b)


class GgufNameProbeTests(unittest.TestCase):
    def test_finds_exact_overflow_without_inference(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"t.gguf"
            p.write_bytes(gguf([b"ok", b"x"*65, b"y"*64]))
            r=m.analyze(p)
            self.assertEqual(r["status"],"GGUF_NAME_LIMIT_VIOLATED")
            self.assertEqual(r["declared_tensors"],3)
            self.assertEqual(r["names_exceeding_64_bytes"],[{"index":1,"bytes":65}])
            self.assertEqual(r["max_tensor_name_bytes"],65)

    def test_all_64_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"t.gguf"
            p.write_bytes(gguf([b"x"*64]))
            self.assertEqual(m.analyze(p)["status"],"NAMES_WITHIN_SPEC")

    def test_refuses_truncated_input(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"t.gguf"
            p.write_bytes(gguf([b"x"*64])[:-3])
            with self.assertRaises(ValueError):
                m.analyze(p)


if __name__=="__main__":
    unittest.main()

"""The public tree carries no personal data: no owner paths, hosts, tailnet/LAN addresses, private config
dirs, emails or the owner's name. Runs over every tracked file (LICENSE excluded).

The pattern is base64-encoded so this guard does not itself publish the strings it forbids.
The Hermes catalog provider id of the Claude subscription plugin (PROVIDER below) is allowed: it is public, not a
personal config dir.
"""

import base64
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERSONAL = re.compile(base64.b64decode(
    "L21udC96ZXIwbW9kZWxzfC93b3Jrc3BhY2UvaGVybWVzLWhvbWV8L2hvbWUva3ZufC9Vc2Vycy9rdm58aG9tZV9rdm58cHJvZmlsZXMvY2xlYW58XGIxMDBcLig2WzQtOV18WzctOV1bMC05XXwxWzAxXVswLTldfDEyWzAtN10pXC5bMC05XStcLlswLTldK3x0c1wubmV0fHRhaWxhMzBkY3xcYjE5MlwuMTY4XC58Y2xhdWRlLXN1Yig/IXNjcmlwdGlvbi1kaXJlY3RzZGstZXhwZXJpbWVudGFsKXxjbGF1ZGUtdWx0cmF8XGJncm9vdFxifGt2bmxvb0B8cHJvdG9uXC5tZXxLZXZpbg=="
).decode())
# Encoded samples of the leaks this guard exists for; each must match.
KNOWN_LEAKS = [base64.b64decode(s).decode() for s in (
    "L21udC96ZXIwbW9kZWxzL3g=",
    "SEVSTUVTX0hPTUU9L3dvcmtzcGFjZS9oZXJtZXMtaG9tZS9wcm9maWxlcy9jbGVhbg==",
    "aG9zdC50YWlsYTMwZGMudHMubmV0",
    "fi8uY2xhdWRlLXN1YjI=",
    "c3NoIGdyb290",
    "S2V2aW4ncyBwaG9uZQ==",
)]
EXCLUDED = {"LICENSE"}
PROVIDER = "-".join(("claude", "subscription", "directsdk", "experimental"))


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [name for name in out.decode().split("\0") if name and name not in EXCLUDED]


class PublicHygiene(unittest.TestCase):
    def test_pattern_catches_the_known_leaks(self):
        for leak in KNOWN_LEAKS + [".".join(("100", "101", "2", "3")), ".".join(("192", "168", "1", "5"))]:
            self.assertRegex(leak, PERSONAL)
        for fine in (PROVIDER, "100.0.0.1", "192.0.2.3", "groots", "~/.hermes"):
            self.assertNotRegex(fine, PERSONAL)

    def test_tracked_files_carry_no_personal_data(self):
        try:
            files = tracked_files()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        hits = []
        for name in files:
            path = ROOT / name
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue  # binary
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if PERSONAL.search(line):
                    hits.append(f"{name}:{number}: {line.strip()[:120]}")
        self.assertEqual(hits, [], "personal data in tracked files:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()

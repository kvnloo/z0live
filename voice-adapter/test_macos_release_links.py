import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin", "macOS release uses BSD mv")
class MacOSReleaseLinksTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(temp)
        self.repo = self.home / "repo"
        deploy = self.repo / "deploy/macos"
        deploy.mkdir(parents=True)
        adapter = self.repo / "voice-adapter"
        adapter.mkdir()
        source = Path(__file__).resolve().parent
        shutil.copy(source / "voice_slots.py", adapter)
        self.script = deploy / "release.sh"
        shutil.copy(source.parent / "deploy/macos/release.sh", self.script)
        self.restart = deploy / "rolling-restart.sh"
        self.restart.write_text("#!/bin/bash\nexit 0\n")
        self.restart.chmod(0o755)
        self.env = {**os.environ, "HOME": temp}
        git = ["git", "-C", str(self.repo)]
        subprocess.run([*git, "init", "-q"], check=True, env=self.env)
        subprocess.run(
            [*git, "-c", "user.name=Release Test", "-c", "user.email=test@example.com",
             "commit", "-qm", "Fixture", "--allow-empty"],
            check=True, env=self.env,
        )
        sha = subprocess.check_output([*git, "rev-parse", "--short", "HEAD"], text=True).strip()
        base = self.home / ".local/share/z0live"
        self.release = base / "releases" / sha
        self.old = base / "releases/old"
        self.old.mkdir(parents=True)
        python = self.release / "voice-adapter/.venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        slots = base / "voice-slots"
        slots.mkdir()
        (slots / "route.json").write_text('{"active": 9168, "phase": "steady"}')
        self.active = slots / "release-9168"
        self.active.symlink_to(self.release)

    def profile_link(self, profile, target):
        link = self.home / ".hermes/profiles" / profile / "plugins/z0live"
        link.parent.mkdir(parents=True)
        if target is None:
            link.mkdir()
        else:
            link.symlink_to(target)
        return link

    def run_release(self):
        return subprocess.run([str(self.script)], env=self.env, capture_output=True, text=True)

    def test_repoints_release_links_and_preserves_other_paths(self):
        first = self.profile_link("first", self.old)
        second = self.profile_link("second profile", "../../../../.local/share/z0live/releases/pruned")
        external = self.profile_link("external", self.old.parent.with_name("releases-other") / "old")
        directory = self.profile_link("directory", None)
        result = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(first.readlink(), self.release)
        self.assertEqual(second.readlink(), self.release)
        self.assertIn(f"Repointed {first}", result.stdout)
        self.assertIn(f"Repointed {second}", result.stdout)
        self.assertTrue(external.is_symlink())
        self.assertEqual(external.readlink(), self.old.parent.with_name("releases-other") / "old")
        self.assertTrue(directory.is_dir())
        self.assertFalse(directory.is_symlink())
        self.assertNotIn("Repointed", self.run_release().stdout)

    def test_failed_roll_or_drain_only_does_not_move_links(self):
        link = self.profile_link("first", self.old)
        self.active.unlink()
        self.active.symlink_to(self.old)
        self.assertNotEqual(self.run_release().returncode, 0)
        self.assertEqual(link.readlink(), self.old)
        self.active.unlink()
        self.active.symlink_to(self.release)
        self.restart.write_text("#!/bin/bash\nexit 1\n")
        self.assertNotEqual(self.run_release().returncode, 0)
        self.assertEqual(link.readlink(), self.old)

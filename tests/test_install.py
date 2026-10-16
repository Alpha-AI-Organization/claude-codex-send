import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.py"
SKILLS = ROOT / "skills"
MAPPING = {"claude": ("codex-send", "claude-receive"), "codex": ("claude-send",)}


def snapshot(root):
    entries = {}
    if not root.exists():
        return entries
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            key = path.relative_to(root).as_posix()
            entries[key] = ("link", os.readlink(path)) if path.is_symlink() else (
                ("dir",) if path.is_dir() else ("file", path.read_bytes()))
    return entries


def source_files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.relative_to(root).parts and p.suffix != ".pyc"}


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.homes = {tool: self.root / tool for tool in MAPPING}
        self.env = os.environ.copy()
        self.env.update(CLAUDE_CONFIG_DIR=str(self.homes["claude"]),
                        CODEX_HOME=str(self.homes["codex"]),
                        HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"))
        for key in ("PYTHONUTF8", "PYTHONIOENCODING"):
            self.env.pop(key, None)

    def run_install(self, *args, script=SCRIPT, env=None):
        return subprocess.run([sys.executable, "-I", "-B", str(script), *args], cwd=self.root,
                              env=env or self.env, capture_output=True, encoding="utf-8", timeout=10)

    def make_homes(self):
        for home in self.homes.values():
            home.mkdir()

    def install(self):
        self.make_homes()
        p = self.run_install()
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def copy_source(self):
        repo = self.root / "repo"
        shutil.copytree(SKILLS, repo / "skills", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(SCRIPT, repo / "install.py")
        return repo

    def symlink(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("directory symlinks unavailable: %s" % e)

    def test_fresh_install_mapping_and_ignored_cache(self):
        repo = self.copy_source()
        cache = repo / "skills/codex-send/__pycache__"
        cache.mkdir()
        (cache / "ignored.pyc").write_bytes(b"cache")
        (repo / "skills/claude-send/ignored.pyc").write_bytes(b"cache")
        if os.name == "nt":
            cache = repo / "skills/claude-receive/__PYCACHE__"
            cache.mkdir()
            (cache / "ignored.PYC").write_bytes(b"cache")
            (repo / "skills/claude-send/EXTRA.PYC").write_bytes(b"cache")
        self.make_homes()
        p = self.run_install(script=repo / "install.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sum(line.startswith("installed ") for line in p.stdout.splitlines()), 3)
        for tool, names in MAPPING.items():
            directory = self.homes[tool] / "skills"
            self.assertEqual({p.name for p in directory.iterdir()}, set(names))
            self.assertIn(str(directory), p.stdout)
            for name in names:
                self.assertEqual(source_files(directory / name), source_files(SKILLS / name))
                self.assertFalse(list((directory / name).rglob("__pycache__")))
                self.assertFalse(list((directory / name).rglob("*.pyc")))
        p = self.run_install(script=repo / "install.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sum(line.startswith("unchanged ") for line in p.stdout.splitlines()), 3)

    def test_second_run_is_unchanged(self):
        self.install()
        before = snapshot(self.root)
        p = self.run_install()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sum(line.startswith("unchanged ") for line in p.stdout.splitlines()), 3)
        self.assertEqual(snapshot(self.root), before)

    def test_conflict_preflight_covers_both_tools_and_force_replaces(self):
        self.install()
        changed = self.homes["codex"] / "skills/claude-send/claude_post.py"
        changed.write_bytes(b"local edit\n")
        missing = self.homes["claude"] / "skills/claude-receive"
        missing.rename(self.root / "saved-receiver")
        before = snapshot(self.root)
        p = self.run_install()
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("use --force to replace", p.stderr)
        self.assertIn(str(changed.parent), p.stderr)
        self.assertEqual(snapshot(self.root), before)
        p = self.run_install("--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sum(line.startswith("replaced ") for line in p.stdout.splitlines()), 1)
        self.assertEqual(sum(line.startswith("installed ") for line in p.stdout.splitlines()), 1)
        for tool, names in MAPPING.items():
            for name in names:
                self.assertEqual(source_files(self.homes[tool] / "skills" / name), source_files(SKILLS / name))

    def test_dry_run_creates_nothing(self):
        before = snapshot(self.root)
        for tool in MAPPING:
            p = self.run_install("--dry-run", "--only", tool)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("would install ", p.stdout)
            self.assertIn(str(self.homes[tool] / "skills"), p.stdout)
        self.assertEqual(snapshot(self.root), before)
        self.make_homes()
        before = snapshot(self.root)
        p = self.run_install("--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sum(line.startswith("would install ") for line in p.stdout.splitlines()), 3)
        self.assertEqual(snapshot(self.root), before)

    def test_default_skips_missing_home_and_only_creates_it(self):
        self.homes["claude"].mkdir()
        p = self.run_install()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skipped codex: %s does not exist (use --only codex to create it)" % self.homes["codex"], p.stdout)
        self.assertFalse(self.homes["codex"].exists())
        p = self.run_install("--only", "codex")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.homes["codex"] / "skills/claude-send/claude_post.py").is_file())

    def test_uninstall_preserves_unrelated_entries_and_is_repeatable(self):
        self.install()
        for home in self.homes.values():
            (home / "skills/unrelated").mkdir()
            (home / "skills/unrelated/keep.txt").write_bytes(b"keep")
            (home / "skills/loose.txt").write_bytes(b"keep")
        before = snapshot(self.root)
        p = self.run_install("--uninstall", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(snapshot(self.root), before)
        for prefix in ("removed ", "absent "):
            p = self.run_install("--uninstall")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(sum(line.startswith(prefix) for line in p.stdout.splitlines()), 3)
            for home in self.homes.values():
                self.assertEqual({p.name for p in (home / "skills").iterdir()}, {"unrelated", "loose.txt"})
                self.assertEqual((home / "skills/unrelated/keep.txt").read_bytes(), b"keep")
                self.assertEqual((home / "skills/loose.txt").read_bytes(), b"keep")

    def test_link_install_unchanged_and_uninstall_preserves_source(self):
        probe = self.root / "probe"
        self.symlink(probe, SKILLS)
        probe.unlink()
        self.make_homes()
        before = snapshot(SKILLS)
        for action in ("installed ", "unchanged "):
            p = self.run_install("--link")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(sum(line.startswith(action) for line in p.stdout.splitlines()), 3)
            for tool, names in MAPPING.items():
                for name in names:
                    dest = self.homes[tool] / "skills" / name
                    self.assertTrue(dest.is_symlink())
                    self.assertEqual(dest.resolve(), (SKILLS / name).resolve())
        p = self.run_install("--uninstall")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(snapshot(SKILLS), before)
        for home in self.homes.values():
            self.assertEqual(list((home / "skills").iterdir()), [])

    def test_unknown_option(self):
        p = self.run_install("--unknown")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unrecognized arguments", p.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_source_prevents_install_and_uninstall(self):
        repo = self.copy_source()
        (repo / "skills/claude-send").rename(repo / "skills/missing-claude-send")
        self.make_homes()
        before = snapshot(self.root)
        for args in ((), ("--uninstall",)):
            p = self.run_install(*args, script=repo / "install.py")
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertIn("source", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertEqual(snapshot(self.root), before)

    def test_force_does_not_follow_destination_links(self):
        self.install()
        outside = self.root / "unrelated-target"
        outside.mkdir()
        (outside / "keep.txt").write_bytes(b"keep")
        dest = self.homes["claude"] / "skills/codex-send"
        (dest / "codex_ctl.py").unlink()
        self.symlink(dest / "nested", outside)
        before = snapshot(outside)
        p = self.run_install("--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(snapshot(outside), before)
        self.assertFalse((dest / "nested").exists())
        removed = self.homes["codex"] / "skills/claude-send"
        removed.rename(self.root / "saved-copy")
        self.symlink(removed, outside)
        p = self.run_install("--force")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(removed.is_symlink())
        self.assertEqual(snapshot(outside), before)

    def test_link_failure_suggests_copy_mode(self):
        runner = ("import runpy,sys; from unittest import mock; "
                  "sys.argv=sys.argv[1:]; "
                  "patch=mock.patch('os.symlink', side_effect=OSError('not permitted')); "
                  "patch.start(); runpy.run_path(sys.argv[0], run_name='__main__')")
        p = subprocess.run([sys.executable, "-I", "-B", "-c", runner, str(SCRIPT),
                            "--only", "codex", "--link"], cwd=self.root, env=self.env,
                           capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(p.returncode, 1)
        self.assertIn("without --link", p.stderr)
        self.assertEqual(len(p.stderr.splitlines()), 1)
        self.assertNotIn("Traceback", p.stderr)

    def test_destination_cannot_overlap_source(self):
        repo = self.copy_source()
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(repo))
        before = snapshot(self.root)
        for args in (("--force",), ("--uninstall",)):
            p = self.run_install("--only", "claude", *args, script=repo / "install.py", env=env)
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertIn("overlaps", p.stderr)
            self.assertEqual(snapshot(self.root), before)

    def test_copy_to_link_requires_force(self):
        self.install()
        before = snapshot(self.root)
        p = self.run_install("--link")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("use --force to replace", p.stderr)
        self.assertEqual(snapshot(self.root), before)

    def test_fallback_homes_are_redirected_to_temp(self):
        env = self.env.copy()
        env.pop("CLAUDE_CONFIG_DIR")
        env.pop("CODEX_HOME")
        for tool, folder in (("claude", ".claude"), ("codex", ".codex")):
            p = self.run_install("--only", tool, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            dest = self.root / "home" / folder / "skills"
            self.assertEqual({p.name for p in dest.iterdir()}, set(MAPPING[tool]))
            self.assertIn(str(dest), p.stdout)


if __name__ == "__main__":
    unittest.main()

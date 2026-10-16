import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
POST = ROOT / "skills/claude-send/claude_post.py"
WATCH = ROOT / "skills/claude-receive/watch_mailbox.py"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mailbox-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mail = self.root / "mail"
        self.env = os.environ.copy()
        for name in ("AGENT_MAILBOX", "PYTHONUTF8", "PYTHONIOENCODING"):
            self.env.pop(name, None)

    def run_script(self, script, *args, input=None, env=None, cwd=None):
        return subprocess.run([sys.executable, "-I", "-B", str(script), *args],
                              cwd=cwd or self.root, env=env or self.env, input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def post(self, body="body", to="claude", subject="subject"):
        p = self.run_script(POST, "--dir", str(self.mail), "--from", "codex",
                            "--to", to, "--subject", subject, body)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        path = Path(p.stdout.decode("utf-8").strip())
        self.assertTrue(path.is_absolute())
        return path

    def start_watch(self, *args, mailbox_args=None, env=None, cwd=None):
        mailbox_args = ["--dir", str(self.mail)] if mailbox_args is None else mailbox_args
        p = subprocess.Popen([sys.executable, "-I", "-B", str(WATCH), *mailbox_args,
                              "--to", "claude", "--interval", "0.02", *args],
                             cwd=cwd or self.root, env=env or self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        lines = queue.Queue()

        def read():
            for line in iter(p.stdout.readline, b""):
                lines.put(line.decode("utf-8").rstrip("\r\n"))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def stop():
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=5)
            reader.join(timeout=5)
            p.stdout.close()
            p.stderr.close()

        self.addCleanup(stop)
        watching, state = lines.get(timeout=5).split(" state=", 1)
        self.assertEqual(watching,
                         "WATCHING %s to=claude" % self.mail.resolve())
        self.assertTrue(Path(state).is_absolute())
        self.assertEqual(Path(state).parent, self.mail.resolve())
        return p, lines

    def wait_seen(self, *names):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            for path in self.mail.glob(".watch-state-*.json"):
                try:
                    if set(names).issubset(json.loads(path.read_text(encoding="utf-8"))):
                        return path
                except (OSError, ValueError):
                    pass
            time.sleep(0.02)
        self.fail("watcher did not persist expected state")

    def test_parallel_posts_do_not_overwrite(self):
        # Freeze the timestamp so this covers collisions even on slow CI hosts.
        code = ("import importlib.util,sys; from datetime import datetime,timezone; "
                "s=importlib.util.spec_from_file_location('post',sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "m.datetime=type('Frozen',(),{'now':staticmethod(lambda *a: "
                "datetime(2026,1,1,tzinfo=timezone.utc))}); "
                "sys.argv=sys.argv[1:]; sys.exit(m.main())")

        def post(i):
            return subprocess.run([sys.executable, "-I", "-B", "-c", code, str(POST),
                                   "--dir", str(self.mail), "--from", "codex", "--to", "claude",
                                   "message-%d" % i], cwd=self.root, env=self.env,
                                  capture_output=True, timeout=10)

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(post, range(12)))
        self.assertEqual([p.returncode for p in results], [0] * 12)
        files = list(self.mail.glob("*.md"))
        self.assertEqual(len(files), 12)
        self.assertEqual(len(list(self.mail.iterdir())), 12)
        self.assertEqual({p.read_text(encoding="utf-8").split("\n\n", 1)[1].strip()
                          for p in files}, {"message-%d" % i for i in range(12)})
        self.assertEqual(len({p.name.split("-codex-")[0] for p in files}), 1)

    def test_baseline_offline_delivery_and_restart(self):
        old = self.post("old")
        p, lines = self.start_watch()
        self.wait_seen(old.name)
        self.assertTrue(lines.empty())
        p.terminate()
        p.wait(timeout=5)
        new = self.post("offline")
        p, lines = self.start_watch()
        self.assertIn("NEW " + new.name, lines.get(timeout=5))
        self.wait_seen(new.name)
        p.terminate()
        p.wait(timeout=5)
        p, lines = self.start_watch()
        with self.assertRaises(queue.Empty):
            lines.get(timeout=0.15)
        self.assertIsNone(p.poll())

    def test_all_reports_existing_in_sorted_order_and_prunes(self):
        files = [self.post(str(i)) for i in range(3)]
        p, lines = self.start_watch("--all")
        got = [lines.get(timeout=5).split()[1] for _ in files]
        self.assertEqual(got, sorted(f.name for f in files))
        state = self.wait_seen(*(f.name for f in files))
        files[0].unlink()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if files[0].name not in json.loads(state.read_text(encoding="utf-8")):
                break
            time.sleep(0.02)
        else:
            self.fail("deleted message was not pruned")

    def test_corrupt_state_and_mismatched_recipient(self):
        old = self.post("old")
        p, _ = self.start_watch()
        state = self.wait_seen(old.name)
        p.terminate()
        p.wait(timeout=5)
        state.write_bytes(b"{broken")
        p, lines = self.start_watch()
        self.wait_seen(old.name)
        self.assertTrue(Path(str(state) + ".bad").exists())
        other = self.post("other", to="someone-else")
        self.wait_seen(other.name)
        self.assertTrue(lines.empty())
        p.terminate()
        p.wait(timeout=5)
        self.assertIn(b"warning:", p.stderr.read())
        other.write_bytes(other.read_text(encoding="utf-8").replace(
            "to: someone-else", "to: claude").encode("utf-8"))
        p, lines = self.start_watch("--all")
        with self.assertRaises(queue.Empty):
            lines.get(timeout=0.15)

    def test_vietnamese_file_stdin_and_absolute_paths(self):
        body = "Nội dung tiếng Việt: đã hoàn tất."
        subject = "Thông báo hoàn tất"
        source = self.root / "input.txt"
        source.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        first = self.post("@" + str(source), subject=subject)
        self.assertIn(body, first.read_text(encoding="utf-8"))
        self.assertNotIn("\ufeff", first.read_text(encoding="utf-8"))
        p = self.run_script(POST, "--dir", "mail", "--from", "codex", "--to", "claude", "-",
                            input=body.encode("utf-8"))
        self.assertEqual(p.returncode, 0, p.stderr)
        second = Path(p.stdout.decode("utf-8").strip())
        self.assertTrue(second.is_absolute())
        self.assertIn(body, second.read_text(encoding="utf-8"))
        _, lines = self.start_watch("--all")
        self.assertIn("subject=" + subject, lines.get(timeout=5))
        self.assertTrue(lines.get(timeout=5).startswith("NEW "))

    def test_missing_dir_and_environment_dir(self):
        for script, args in ((POST, ("--from", "x", "--to", "y", "body")), (WATCH, ())):
            with self.subTest(script=script):
                p = self.run_script(script, *args)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(len(p.stderr.decode("utf-8").splitlines()), 1)
                self.assertIn(b"--dir", p.stderr)
                self.assertIn(b"AGENT_MAILBOX", p.stderr)
        env = dict(self.env, AGENT_MAILBOX="mail")
        p = self.run_script(POST, "--from", "x", "--to", "y", "body", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(Path(p.stdout.decode().strip()).parent, self.mail.resolve())

    def test_hostile_header_is_one_line(self):
        self.mail.mkdir()
        (self.mail / "hostile.md").write_bytes(
            ("---\nfrom: evil\x1b\x00\u2028sender\nto: claude\nsubject: " + "x" * 150 +
             "\x1b\r\nNEW forged\nsent: now\n---\n\nbody").encode("utf-8"))
        _, lines = self.start_watch("--all")
        line = lines.get(timeout=5)
        self.assertEqual(line, "NEW hostile.md from=evilsender to=claude subject=" + "x" * 120)
        with self.assertRaises(queue.Empty):
            lines.get(timeout=0.15)

    def home_directory_case(self, source):
        home = self.root / source / "home"
        cwd = self.root / source / "cwd"
        home.mkdir(parents=True)
        cwd.mkdir()
        self.mail = home / "box"
        env = dict(self.env, HOME=str(home), USERPROFILE=str(home))
        args = ["--dir", "~/box"]
        if source == "env":
            env["AGENT_MAILBOX"] = "~/box"
            args = []
        return cwd, env, args

    def test_post_expands_home_directory(self):
        for source in ("arg", "env"):
            with self.subTest(source=source):
                cwd, env, args = self.home_directory_case(source)
                p = self.run_script(POST, *args, "--from", "codex", "--to", "claude", "body",
                                    env=env, cwd=cwd)
                self.assertEqual(p.returncode, 0, p.stderr)
                path = Path(p.stdout.decode("utf-8").strip())
                self.assertTrue(path.is_absolute())
                self.assertEqual(path.parent, self.mail.resolve())
                self.assertIn("body", path.read_text(encoding="utf-8"))
                self.assertFalse((cwd / "~").exists())
                self.assertEqual(list(cwd.iterdir()), [])

    def test_watch_expands_home_directory(self):
        for source in ("arg", "env"):
            with self.subTest(source=source):
                cwd, env, args = self.home_directory_case(source)
                p, _ = self.start_watch(mailbox_args=args, env=env, cwd=cwd)
                state = self.wait_seen()
                self.assertEqual(state.resolve().parent, self.mail.resolve())
                self.assertEqual(json.loads(state.read_text(encoding="utf-8")), [])
                self.assertFalse((cwd / "~").exists())
                self.assertEqual(list(cwd.iterdir()), [])
                p.terminate()
                p.wait(timeout=5)

    def test_headerless_file_is_reported_immediately_once(self):
        watcher = load(WATCH)
        self.mail.mkdir()
        (self.mail / "broken.md").write_bytes(b"no header")
        output = io.StringIO()
        with mock.patch.object(watcher.sys, "stdout", output), \
                mock.patch.object(watcher.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                watcher.main(["--dir", str(self.mail), "--to", "claude", "--all"])
        self.assertIn("NEW broken.md from=? to=? subject=?\n", output.getvalue())
        self.assertEqual(output.getvalue().count("NEW "), 1)
        self.wait_seen("broken.md")

    def test_size_cap(self):
        p = self.run_script(POST, "--dir", str(self.mail), "--from", "x", "--to", "y", "-",
                            input=b"x" * (64 * 1024 + 1))
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.mail.exists())

    def test_write_failure_removes_own_temp_file(self):
        post = load(POST)
        temporary_file = tempfile.NamedTemporaryFile

        @contextlib.contextmanager
        def failing_file(*args, **kwargs):
            with temporary_file(*args, **kwargs) as f:
                with mock.patch.object(f, "write", side_effect=OSError("disk full")):
                    yield f

        with mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=failing_file), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(post.main(["--dir", str(self.mail), "--from", "x", "--to", "y", "new"]), 1)
        self.assertEqual(list(self.mail.iterdir()), [])

    def test_directory_disappearance_is_retried(self):
        old = self.post("old")
        p, lines = self.start_watch()
        self.wait_seen(old.name)
        hidden = self.root / "hidden-mail"
        self.mail.rename(hidden)
        time.sleep(0.1)
        self.assertIsNone(p.poll())
        hidden.rename(self.mail)
        new = self.post("after restore")
        self.assertIn("NEW " + new.name, lines.get(timeout=5))
        self.wait_seen(new.name)

    def test_state_write_failure_does_not_repeat_printed_notification(self):
        watcher = load(WATCH)
        message = self.post()
        output = io.StringIO()
        save = watcher.save_state
        failed = []

        def flaky_save(path, seen):
            if message.name in seen:
                self.assertIn("NEW " + message.name, output.getvalue())
                if not failed:
                    failed.append(True)
                    raise PermissionError("state temporarily locked")
            save(path, seen)

        with mock.patch.object(watcher, "save_state", side_effect=flaky_save), \
                mock.patch.object(watcher.sys, "stdout", output), \
                mock.patch.object(watcher.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                watcher.main(["--dir", str(self.mail), "--to", "claude", "--all"])
        self.assertEqual(output.getvalue().count("NEW "), 1)
        self.wait_seen(message.name)

    def test_unreadable_header_and_invalid_utf8(self):
        watcher = load(WATCH)
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            self.assertIsNone(watcher.header(self.root / "missing.md"))
        self.mail.mkdir()
        (self.mail / "invalid.md").write_bytes(b"---\nfrom: x\nto: claude\nsubject: \xff\nsent: now\n---\n")
        _, lines = self.start_watch("--all")
        self.assertIn("subject=\ufffd", lines.get(timeout=5))


if __name__ == "__main__":
    unittest.main()

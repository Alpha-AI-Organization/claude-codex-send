import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/codex-send/codex_ctl.py"
FAKE = ROOT / "tests/fake_codex_proxy.py"
spec = importlib.util.spec_from_file_location("codex_ctl", SCRIPT)
ctl = importlib.util.module_from_spec(spec)
old = sys.dont_write_bytecode
sys.dont_write_bytecode = True
try:
    spec.loader.exec_module(ctl)
finally:
    sys.dont_write_bytecode = old

RUNNER = """
import functools, importlib.util, sys
spec = importlib.util.spec_from_file_location('codex_ctl', sys.argv[1])
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
ctl.Client = functools.partial(ctl.Client, cmd=[sys.executable, '-I', '-B', sys.argv[2],
                                               '--scenario', sys.argv[3], '--log', sys.argv[4]])
sys.exit(ctl.main(sys.argv[5:]))
"""


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-proxy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.txt"
        self.env = os.environ.copy()
        for name in ("CODEX_BIN", "PYTHONUTF8", "PYTHONIOENCODING"):
            self.env.pop(name, None)

    def client(self, scenario="normal"):
        c = ctl.Client(cmd=[sys.executable, "-I", "-B", str(FAKE), "--scenario", scenario,
                            "--log", str(self.log)])
        self.addCleanup(c.close)
        return c

    def run_cli(self, *args, scenario="normal", input=None):
        return subprocess.run([sys.executable, "-I", "-B", "-c", RUNNER, str(SCRIPT), str(FAKE),
                               scenario, str(self.log), *args], cwd=self.root, env=self.env,
                              input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    def assert_error(self, p, code):
        self.assertEqual(p.returncode, code, p.stderr.decode("utf-8", errors="replace"))
        error = p.stderr.decode("utf-8")
        self.assertTrue(error.startswith("error: "), error)
        self.assertEqual(len(error.splitlines()), 1, error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn(b"REPLY:", p.stdout)

    def test_normal_send_and_list(self):
        p = self.run_cli("send", "live", "hello", "--wait", "5")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"REPLY: Reply: hello", p.stdout)
        p = self.run_cli("list")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"live | idle | Test | fake", p.stdout)

    def test_ping_and_fragmented_and_binary(self):
        for scenario in ("ping", "fragmented", "binary"):
            with self.subTest(scenario=scenario):
                c = self.client(scenario)
                self.assertEqual(c.call("echo", {"text": "tiếng Việt"}), {"text": "tiếng Việt"})
                c.close()
        self.assertIn("masked-pong", self.log.read_text(encoding="utf-8"))

    def test_extended_frame_lengths_both_directions(self):
        c = self.client()
        for length in (10, 126, 65536, 70000):
            with self.subTest(length=length):
                payload = {"text": "x" * length}
                self.assertEqual(c.call("echo", payload), payload)

    def test_new_turn_without_agent_does_not_reuse_old_reply(self):
        p = self.run_cli("send", "live", "hello", "--wait", "5", scenario="no-agent")
        self.assert_error(p, 4)
        self.assertNotIn(b"OLD REPLY", p.stdout + p.stderr)

    def test_stale_idle_and_in_progress_turn_are_not_final(self):
        for scenario in ("stale-idle", "idle-in-progress"):
            with self.subTest(scenario=scenario):
                p = self.run_cli("send", "live", "hello", "--wait", "6", scenario=scenario)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(b"REPLY: Reply: hello", p.stdout)

    def test_busy_thread_does_not_send(self):
        p = self.run_cli("send", "live", "hello", scenario="busy")
        self.assert_error(p, 3)
        self.assertNotIn("turn/start", self.log.read_text(encoding="utf-8"))

    def test_turn_start_transport_failures_are_ambiguous(self):
        for scenario in ("start-drop", "start-bad-json", "start-missing-result", "start-invalid-result"):
            with self.subTest(scenario=scenario):
                p = self.run_cli("send", "live", "hello", "--wait", "5", scenario=scenario)
                self.assert_error(p, 4)
                self.assertEqual(p.stderr.decode("utf-8").strip(),
                                 "error: turn/start did not complete; the message may have been delivered, "
                                 "check the thread before sending again")
                self.assertIn("turn/start", self.log.read_text(encoding="utf-8"))

    def test_turn_start_server_refusal_keeps_exit_five(self):
        p = self.run_cli("send", "live", "hello", scenario="start-refused")
        self.assert_error(p, 5)
        self.assertEqual(p.stderr.decode("utf-8").strip(), "error: turn explicitly refused")
        self.assertIn("turn/start", self.log.read_text(encoding="utf-8"))

    def test_turn_start_timeout_and_write_errors_are_ambiguous(self):
        for error in (TimeoutError("no response"), ConnectionError("disconnected"), OSError("write failed")):
            with self.subTest(error=type(error).__name__):
                c = mock.Mock()
                c.call.side_effect = [{"thread": {"status": {"type": "idle"}, "turns": []}}, error]
                with self.assertRaises(ctl.CommandError) as caught:
                    ctl.cmd_send(c, "live", "hello", 5)
                self.assertEqual(caught.exception.code, 4)
                self.assertEqual(str(caught.exception),
                                 "turn/start did not complete; the message may have been delivered, "
                                 "check the thread before sending again")
                self.assertEqual(c.call.call_count, 2)
                self.assertEqual(c.call.call_args[0][0], "turn/start")

    def test_unknown_thread(self):
        p = self.run_cli("send", "missing", "hello")
        self.assert_error(p, 5)
        self.assertIn(b"thread missing not found or unreadable: unknown thread not readable", p.stderr)
        self.assertNotIn("turn/start", self.log.read_text(encoding="utf-8"))

    def test_timeout_and_stalled_read_respect_wait(self):
        for scenario in ("timeout", "stalled-read"):
            with self.subTest(scenario=scenario):
                start = time.monotonic()
                p = self.run_cli("send", "live", "hello", "--wait", "0.15", scenario=scenario)
                self.assert_error(p, 4)
                self.assertLess(time.monotonic() - start, 3)
                self.assertIn("turn/start", self.log.read_text(encoding="utf-8"))

    def test_proxy_stderr_is_drained_and_reported(self):
        p = self.run_cli("list", scenario="startup-error")
        self.assert_error(p, 5)
        self.assertIn(b"proxy startup failed", p.stderr)
        self.assertLess(len(p.stderr), 4500)

    def test_close_and_bad_protocol_are_clean_errors(self):
        for scenario in ("close", "bad-json", "bad-handshake"):
            with self.subTest(scenario=scenario):
                self.assert_error(self.run_cli("list", scenario=scenario), 5)

    def test_utf8_bom_file_and_stdin(self):
        body = "Đã hoàn tất kiểm tra"
        path = self.root / "message.txt"
        path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        for text, data in (("@" + str(path), None), ("-", body.encode("utf-8"))):
            with self.subTest(text=text):
                p = self.run_cli("send", "live", text, "--wait", "5", input=data)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("REPLY: Reply: " + body, p.stdout.decode("utf-8"))
                self.assertNotIn("\ufeff", p.stdout.decode("utf-8"))

    def test_argument_errors_do_not_start_proxy(self):
        for args in ((), ("unknown",), ("send",), ("send", "live", "hi", "--wait"),
                     ("send", "live", "hi", "--wait", "-1"),
                     ("send", "live", "hi", "--wait", "nan"),
                     ("send", "live", "hi", "--wait", "inf"),
                     ("send", "live", "@missing.txt")):
            with self.subTest(args=args):
                self.assert_error(self.run_cli(*args), 2)
        self.assert_error(self.run_cli("send", "live", "-", input=b"x" * 8001), 2)
        self.assertFalse(self.log.exists())

    def test_help_documents_exit_codes(self):
        p = self.run_cli("--help")
        self.assertEqual(p.returncode, 0)
        for code in (b"0 =", b"2 =", b"3 =", b"4 =", b"5 ="):
            self.assertIn(code, p.stdout)
        self.assertIn("4 = sent (or possibly sent) but no reply in time, turn/start did not complete, "
                      "or the thread finished without an agent message", " ".join(p.stdout.decode("utf-8").split()))
        self.assertEqual(self.run_cli("send", "--help").returncode, 0)
        self.assertFalse(self.log.exists())

    def test_close_reaps_process(self):
        c = self.client()
        c.close()
        self.assertIsNotNone(c.p.poll())
        c.close()

    def test_non_dict_turn_is_a_clean_protocol_error(self):
        self.assert_error(self.run_cli("send", "live", "hi", "--wait", "5", scenario="bad-turn"), 5)

    def test_close_kills_process_when_terminate_times_out(self):
        c = object.__new__(ctl.Client)
        c._closed = False
        c.p = mock.Mock()
        c.p.poll.return_value = None
        c.p.wait.side_effect = [subprocess.TimeoutExpired("fake", 3), 0]
        c.close()
        c.p.terminate.assert_called_once_with()
        c.p.kill.assert_called_once_with()
        self.assertEqual(c.p.wait.call_args_list, [mock.call(timeout=3), mock.call()])
        c.close()


if __name__ == "__main__":
    unittest.main()

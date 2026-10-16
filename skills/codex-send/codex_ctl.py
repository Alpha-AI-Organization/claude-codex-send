"""Send a message to a live Codex thread and read its reply.

  python codex_ctl.py list
  python codex_ctl.py send <thread_id> <text | @file.txt | -> [--wait 120]

Transport: `codex app-server proxy` pipes bytes to the local app-server control socket, which
speaks WebSocket carrying JSON-RPC. Standard library only. Needs codex on PATH, or CODEX_BIN.

Exit codes: 0 = success (send prints REPLY: <text>; list prints threads);
2 = usage/argument error; 3 = thread not idle, nothing sent;
4 = sent (or possibly sent) but no reply in time, turn/start did not complete,
or the thread finished without an agent message;
5 = connection/protocol/thread-not-found error.
"""
import argparse, base64, json, math, os, queue, shutil, struct, subprocess, sys, threading, time
import unicodedata

MAX_CHARS = 8000
EXIT_CODES = __doc__.split("Exit codes: ", 1)[1]


def one_line(value):
    return " ".join("".join(c for c in str(value) if not unicodedata.category(c).startswith("C")
                            or c in "\r\n\t").split())


class CommandError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ServerError(RuntimeError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "error: " + one_line(message) + "\n")


class Client:
    def __init__(self, cmd=None):
        if cmd is None:
            codex = os.environ.get("CODEX_BIN") or shutil.which("codex")
            if not codex:
                raise RuntimeError("codex not found on PATH (set CODEX_BIN to its full path)")
            cmd = [codex, "app-server", "proxy"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q = queue.Queue()
        self.buf = b""
        self._id = 0
        self._closed = False
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._deadline = time.monotonic() + 15
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._errors = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()
        self._errors.start()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self._write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
            self._handshake()
            self.call("initialize", {"clientInfo": {"name": "codex-send", "title": "codex-send", "version": "1"}})
            self._send({"jsonrpc": "2.0", "method": "initialized"})
        except BaseException as e:
            self.close()
            if isinstance(e, Exception):
                raise RuntimeError(self.error_message(e)) from e
            raise

    def _write(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def _pump(self):
        try:
            while True:
                chunk = os.read(self.p.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.q.put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.q.put(None)

    def _drain_stderr(self):
        try:
            while True:
                chunk = os.read(self.p.stderr.fileno(), 4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr.extend(chunk)
                    del self._stderr[:-4096]
        except (OSError, ValueError):
            pass

    def error_message(self, error):
        with self._stderr_lock:
            detail = self._stderr.decode("utf-8", errors="replace").strip()
        return "%s; proxy: %s" % (error, detail) if detail else str(error)

    def _fill(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no data from the Codex app-server (is a Codex session running?)")
        try:
            chunk = self.q.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("no data from the Codex app-server (is a Codex session running?)") from None
        if chunk is None:
            raise EOFError("proxy closed; is the Codex app-server daemon running?")
        self.buf += chunk

    def _handshake(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"101":
            raise RuntimeError("websocket upgrade refused: " + one_line(head.decode(errors="replace")))

    def _take(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, op, data):
        mask = os.urandom(4)
        n = len(data)
        first = bytes([0x80 | op])
        if n < 126:
            hdr = first + bytes([0x80 | n])
        elif n < 65536:
            hdr = first + b"\xfe" + struct.pack(">H", n)
        else:
            hdr = first + b"\xff" + struct.pack(">Q", n)
        self._write(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _send(self, obj):
        self._send_frame(1, json.dumps(obj).encode("utf-8"))

    def _recv(self):
        fragments, kind = [], None
        while True:
            if time.monotonic() >= self._deadline:
                raise TimeoutError("no JSON-RPC response before the deadline")
            b1, b2 = self._take(2)
            op, fin = b1 & 15, bool(b1 & 0x80)
            n = b2 & 127
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            data = self._take(n)
            if op == 8:
                raise EOFError("websocket connection closed")
            if op == 9:
                self._send_frame(10, data)
                continue
            if op == 10:
                continue
            if op in (1, 2):
                if kind is not None:
                    raise RuntimeError("new websocket message before final continuation")
                kind = op
            elif op != 0 or kind is None:
                raise RuntimeError("unexpected websocket opcode %s" % op)
            if kind == 1:
                fragments.append(data)
            if fin:
                if kind == 1:
                    message = json.loads(b"".join(fragments).decode("utf-8"))
                    if not isinstance(message, dict):
                        raise RuntimeError("JSON-RPC message must be an object")
                    return message
                kind = None

    def call(self, method, params=None, timeout=15):
        self._deadline = time.monotonic() + timeout
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            m = self._recv()
            if m.get("id") == self._id:
                if "error" in m:
                    error = m["error"]
                    raise ServerError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
                if "result" not in m:
                    raise RuntimeError("JSON-RPC response has no result")
                return m["result"]

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.p.poll() is None:
            self.p.terminate()
        try:
            self.p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


def thread_info(c, tid, turns=False, timeout=15):
    try:
        result = c.call("thread/read", {"threadId": tid, "includeTurns": turns}, timeout=timeout)
    except ServerError as e:
        raise ServerError("thread %s not found or unreadable: %s" % (tid, e)) from e
    if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
        raise RuntimeError("invalid thread/read response")
    thread = result["thread"]
    status = thread.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("type"), str):
        raise RuntimeError("invalid thread status in thread/read response")
    return thread


def last_agent_text(thread, before):
    for turn in reversed((thread.get("turns") or [])[before:]):
        texts = [it.get("text") for it in (turn.get("items") or [])
                 if str(it.get("type", "")).lower() in ("agentmessage", "agent_message") and it.get("text")]
        if texts:
            return texts[-1]
    return None


def cmd_list(c):
    result = c.call("thread/loaded/list", {})
    for tid in result["data"]:
        t = thread_info(c, tid)
        print("%s | %s | %s | %s" % (tid, (t.get("status") or {}).get("type"), t.get("name"), t.get("cwd")))


def cmd_send(c, tid, text, wait):
    t = thread_info(c, tid, True)
    status = (t.get("status") or {}).get("type")
    if status != "idle":
        raise CommandError(3, "thread is %s, not idle; not sending (it would interrupt its work). Try later." % status)
    before = len(t.get("turns") or [])
    try:
        result = c.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": text, "text_elements": []}]})
        if (not isinstance(result, dict) or not isinstance(result.get("turn"), dict)
                or not isinstance(result["turn"].get("id"), str) or not result["turn"]["id"]):
            raise RuntimeError("invalid turn/start response")
    except ServerError:
        raise
    except (OSError, EOFError, RuntimeError, ValueError, KeyError, TypeError) as e:
        raise CommandError(4, "turn/start did not complete; the message may have been delivered, "
                           "check the thread before sending again") from e
    print("sent; waiting up to %gs for the thread to finish" % wait, flush=True)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            t = thread_info(c, tid, True, timeout=max(0, end - time.monotonic()))
        except TimeoutError:
            break
        new = (t.get("turns") or [])[before:]
        if time.monotonic() >= end:
            break
        if ((t.get("status") or {}).get("type") == "idle" and new
                and not any(turn.get("status") in ("inProgress", "in_progress", "running") for turn in new)):
            reply = last_agent_text(t, before)
            if reply is None:
                raise CommandError(4, "thread finished without an agent message in the new turns")
            print("REPLY:", reply)
            return
        time.sleep(min(2, max(0, end - time.monotonic())))
    raise CommandError(4, "no final reply within %gs; the thread may still be working, check its window" % wait)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = Parser(description=__doc__.split("Exit codes:", 1)[0], epilog=EXIT_CODES,
                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list loaded threads",
                   formatter_class=argparse.RawDescriptionHelpFormatter)
    send = sub.add_parser("send", help="send to an idle thread and wait for its reply",
                          formatter_class=argparse.RawDescriptionHelpFormatter)
    send.add_argument("thread_id")
    send.add_argument("text", help="message text, @file to read UTF-8, or - for UTF-8 stdin")
    send.add_argument("--wait", type=float, default=120, metavar="N", help="seconds to wait (default: 120)")
    a = ap.parse_args(argv)
    if a.command == "send":
        if not math.isfinite(a.wait) or a.wait < 0:
            ap.error("--wait must be a nonnegative finite number")
        try:
            if a.text == "-":
                text = sys.stdin.buffer.read().decode("utf-8") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
            elif a.text.startswith("@"):
                with open(a.text[1:], encoding="utf-8-sig") as f:
                    text = f.read()
            else:
                text = a.text
        except (OSError, UnicodeError, ValueError) as e:
            ap.error(str(e))
        if len(text) > MAX_CHARS:
            ap.error("message is %d characters; keep it under %d and point to a file instead" % (len(text), MAX_CHARS))
    c = None
    try:
        c = Client()
        if a.command == "list":
            cmd_list(c)
        else:
            cmd_send(c, a.thread_id, text, a.wait)
        return 0
    except CommandError as e:
        print("error: " + one_line(e), file=sys.stderr)
        return e.code
    except (OSError, EOFError, RuntimeError, ValueError, KeyError, TypeError, AttributeError) as e:
        if c is not None:
            c.close()
        print("error: " + one_line(c.error_message(e) if c is not None else e), file=sys.stderr)
        return 5
    finally:
        if c is not None:
            c.close()


if __name__ == "__main__":
    sys.exit(main())

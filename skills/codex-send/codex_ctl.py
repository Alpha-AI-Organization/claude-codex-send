"""Send a message to a live Codex thread and read its reply.

  python codex_ctl.py list
  python codex_ctl.py send <thread_id> <text | @file.txt> [--wait 120]

Transport: `codex app-server proxy` pipes bytes to the local app-server control socket, which
speaks WebSocket carrying JSON-RPC. The proxy is used so the script needs no Unix-socket support
(Python on Windows has none). Standard library only. Needs `codex` on PATH, or set CODEX_BIN.
"""
import base64, json, os, queue, shutil, struct, subprocess, sys, threading, time

MAX_CHARS = 8000


class Client:
    def __init__(self):
        codex = os.environ.get("CODEX_BIN") or shutil.which("codex")
        if not codex:
            sys.exit("codex not found on PATH (set CODEX_BIN to its full path)")
        self.p = subprocess.Popen([codex, "app-server", "proxy"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.q = queue.Queue()
        self.buf = b""
        self._id = 0
        threading.Thread(target=self._pump, daemon=True).start()
        key = base64.b64encode(os.urandom(16)).decode()
        self._write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        self._handshake()
        self.call("initialize", {"clientInfo": {"name": "codex-send", "title": "codex-send", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def _write(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def _pump(self):
        while True:
            chunk = os.read(self.p.stdout.fileno(), 65536)
            self.q.put(chunk or None)
            if not chunk:
                return

    def _fill(self, timeout=15):
        try:
            chunk = self.q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no data from the Codex app-server (is a Codex session running?)")
        if chunk is None:
            raise EOFError("proxy closed; is the Codex app-server daemon running?")
        self.buf += chunk

    def _handshake(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket upgrade refused: " + head.decode(errors="replace"))

    def _take(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = b"\x81" + bytes([0x80 | n])
        elif n < 65536:
            hdr = b"\x81\xfe" + struct.pack(">H", n)
        else:
            hdr = b"\x81\xff" + struct.pack(">Q", n)
        self._write(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self):
        while True:
            b1, b2 = self._take(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            data = self._take(n)
            op = b1 & 0x0F
            if op == 1:
                return json.loads(data.decode())
            if op == 8:
                raise EOFError("connection closed")

    def call(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            m = self._recv()
            if m.get("id") == self._id:
                if "error" in m:
                    raise RuntimeError(json.dumps(m["error"]))
                return m.get("result")

    def close(self):
        self.p.terminate()


def thread_info(c, tid, turns=False):
    return c.call("thread/read", {"threadId": tid, "includeTurns": turns})["thread"]


def last_agent_text(thread):
    for turn in reversed(thread.get("turns") or []):
        texts = [it.get("text") for it in (turn.get("items") or [])
                 if str(it.get("type", "")).lower() in ("agentmessage", "agent_message") and it.get("text")]
        if texts:
            return texts[-1]
    return None


def cmd_list(c):
    for tid in c.call("thread/loaded/list", {})["data"]:
        t = thread_info(c, tid)
        print("%s | %s | %s | %s" % (tid, (t.get("status") or {}).get("type"), t.get("name"), t.get("cwd")))


def cmd_send(c, tid, text, wait):
    if len(text) > MAX_CHARS:
        sys.exit("message is %d characters; keep it under %d and point to a file instead" % (len(text), MAX_CHARS))
    status = (thread_info(c, tid).get("status") or {}).get("type")
    if status != "idle":
        sys.exit("thread is %s, not idle; not sending (it would interrupt its work). Try later." % status)
    before = len(thread_info(c, tid, True).get("turns") or [])
    c.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": text, "text_elements": []}]})
    print("sent; waiting up to %ss for the thread to finish" % wait)
    end = time.time() + wait
    while time.time() < end:
        time.sleep(2)
        t = thread_info(c, tid, True)
        if (t.get("status") or {}).get("type") == "idle" and len(t.get("turns") or []) > before:
            print("REPLY:", last_agent_text(t))
            return
    print("no final reply within %ss; the thread may still be working, check its window" % wait)


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not argv or argv[0] not in ("list", "send"):
        sys.exit(__doc__)
    if argv[0] == "send" and len(argv) < 3:
        sys.exit(__doc__)
    c = Client()
    try:
        if argv[0] == "list":
            cmd_list(c)
        else:
            wait = int(argv[argv.index("--wait") + 1]) if "--wait" in argv else 120
            text = open(argv[2][1:], encoding="utf-8").read() if argv[2].startswith("@") else argv[2]
            cmd_send(c, argv[1], text, wait)
    finally:
        c.close()


if __name__ == "__main__":
    main(sys.argv[1:])

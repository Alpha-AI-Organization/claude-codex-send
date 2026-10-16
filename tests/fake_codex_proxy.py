"""A stdio-only WebSocket/JSON-RPC proxy for local tests; never opens a socket."""
import argparse, base64, hashlib, json, struct, sys


def take(n):
    data = bytearray()
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def frame(op, data, fin=True):
    size = len(data)
    first = bytes([(0x80 if fin else 0) | op])
    if size < 126:
        head = first + bytes([size])
    elif size < 65536:
        head = first + b"\x7e" + struct.pack(">H", size)
    else:
        head = first + b"\x7f" + struct.pack(">Q", size)
    sys.stdout.buffer.write(head + data)
    sys.stdout.buffer.flush()


def read_frame():
    b1, b2 = take(2)
    if not b2 & 0x80:
        raise ValueError("client frame is not masked")
    n = b2 & 127
    if n == 126:
        n = struct.unpack(">H", take(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", take(8))[0]
    mask = take(4)
    data = take(n)
    return b1 & 15, bytes(c ^ mask[i % 4] for i, c in enumerate(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="normal")
    ap.add_argument("--log")
    a = ap.parse_args()
    if a.scenario == "startup-error":
        sys.stderr.buffer.write(b"x" * 100000 + b"\nproxy startup failed\n")
        sys.stderr.buffer.flush()
        return
    request = bytearray()
    while not request.endswith(b"\r\n\r\n"):
        request.extend(take(1))
    headers = {}
    for line in bytes(request).decode("ascii").split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.lower()] = value.strip()
    accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] +
                                "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    status = "403 Forbidden" if a.scenario == "bad-handshake" else "101 Switching Protocols"
    sys.stdout.buffer.write(("HTTP/1.1 %s\r\nUpgrade: websocket\r\n"
                             "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % (status, accept)).encode())
    sys.stdout.buffer.flush()
    started, reads, incoming = False, 0, ""

    def log(value):
        if a.log:
            with open(a.log, "a", encoding="utf-8", newline="\n") as f:
                f.write(value + "\n")

    def ping():
        frame(9, b"check-pong")
        op, payload = read_frame()
        if (op, payload) != (10, b"check-pong"):
            raise ValueError("missing or incorrect pong")
        log("masked-pong")

    while True:
        op, payload = read_frame()
        if op != 1:
            continue
        msg = json.loads(payload.decode("utf-8"))
        method = msg["method"]
        log(method)
        if "id" not in msg:
            continue
        result, error = {}, None
        if method == "thread/loaded/list":
            result = {"data": ["live"]}
        elif method == "thread/read":
            if msg["params"]["threadId"] != "live":
                error = {"code": -32000, "message": "unknown thread\nnot readable"}
            else:
                turns = [{"id": "old", "status": "completed", "items": [
                    {"type": "agentMessage", "text": "OLD REPLY MUST NOT LEAK"}]}]
                status = "active" if a.scenario == "busy" else "idle"
                if started:
                    reads += 1
                    stale = a.scenario == "stale-idle" and reads == 1
                    if a.scenario in ("timeout", "stalled-read"):
                        status = "active"
                    if a.scenario == "stalled-read":
                        # Wait for termination without writing a response.
                        take(1)
                    if not stale:
                        turn_status = "inProgress" if status == "active" else "completed"
                        items = [] if a.scenario == "no-agent" else [
                            {"type": "agent_message", "text": "Reply: " + incoming}]
                        if a.scenario == "idle-in-progress" and reads == 1:
                            turn_status, items = "inProgress", []
                        turns.append({"id": "new", "status": turn_status, "items": items})
                result = {"thread": {"status": {"type": status}, "name": "Test", "cwd": "fake",
                                     "turns": turns if msg["params"].get("includeTurns") else []}}
                if started and a.scenario == "bad-turn":
                    result["thread"]["turns"][-1] = "invalid"
        elif method == "turn/start":
            if a.scenario == "start-drop":
                return
            if a.scenario == "start-bad-json":
                frame(1, b"{invalid")
                continue
            if a.scenario == "start-missing-result":
                frame(1, json.dumps({"jsonrpc": "2.0", "id": msg["id"]}).encode("utf-8"))
                continue
            if a.scenario == "start-refused":
                frame(1, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32000, "message": "turn explicitly refused"}}).encode("utf-8"))
                continue
            started = True
            incoming = msg["params"]["input"][0]["text"]
            result = None if a.scenario == "start-invalid-result" else {"turn": {"id": "new"}}
        elif method == "echo":
            result = msg["params"]
        elif method != "initialize":
            error = {"code": -32601, "message": "unknown method"}
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        reply["error" if error else "result"] = error if error else result
        data = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        if a.scenario == "close":
            frame(8, struct.pack(">H", 1000))
            return
        if a.scenario == "bad-json":
            frame(1, b"{invalid")
            continue
        if a.scenario == "ping":
            ping()
        if a.scenario == "fragmented":
            split = next((i + 1 for i, byte in enumerate(data) if byte >= 128), max(1, len(data) // 2))
            frame(1, data[:split], False)
            ping()
            frame(10, b"unsolicited pong")
            frame(0, data[split:])
        else:
            if a.scenario == "binary":
                frame(2, b"ignore", False)
                ping()
                frame(0, b"this too")
            frame(1, data)


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        pass

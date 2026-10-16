"""Print new mailbox messages for Claude Code's Monitor tool, with state across restarts.

  python -u skills/claude-receive/watch_mailbox.py [--dir DIR] [--to NAME] [--interval 2] [--all]

Set the mailbox directory with --dir or AGENT_MAILBOX. First startup baselines existing mail;
--all also reports unseen existing mail. State is saved inside the mailbox after each notification.
Only the header is printed; open the file to read the body. Standard library only.
"""
import argparse, hashlib, json, math, os, sys, tempfile, time


def safe_value(value, limit=120):
    return "".join(ch for ch in str(value) if ch.isprintable())[:limit]


def header(path):
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return None
            for line in f:
                if line.strip() == "---":
                    return out if all(k in out for k in ("from", "to", "subject", "sent")) else None
                k, sep, v = line.partition(":")
                if sep:
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return None


def save_state(path, seen):
    fd, tmp = tempfile.mkstemp(prefix=".watch-state-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sorted(seen), f, ensure_ascii=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def load_state(path, baseline):
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
        if not isinstance(seen, list) or not all(isinstance(n, str) for n in seen):
            raise ValueError("state must be a list of file names")
        return set(seen)
    except FileNotFoundError:
        return set(baseline)
    except (ValueError, UnicodeError):
        os.replace(path, path + ".bad")
        print("warning: corrupt mailbox state backed up to " + safe_value(path + ".bad", None),
              file=sys.stderr, flush=True)
        return set(baseline)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("AGENT_MAILBOX"))
    ap.add_argument("--to", default="", help="only report messages addressed to this name (or 'all')")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--all", action="store_true", help="report unseen files even on the first start")
    a = ap.parse_args(argv)
    if not a.dir:
        ap.exit(2, "error: set the mailbox directory with --dir or AGENT_MAILBOX\n")
    if not math.isfinite(a.interval) or a.interval <= 0:
        ap.exit(2, "error: --interval must be a positive finite number\n")
    directory = os.path.realpath(os.path.expanduser(a.dir))
    # A digest keeps distinct recipient filters separate without allowing path separators.
    key = "to-" + hashlib.sha256(a.to.encode("utf-8")).hexdigest() if a.to else "all"
    state = os.path.join(directory, ".watch-state-%s.json" % key)
    print("WATCHING %s to=%s state=%s" % (safe_value(directory, None), safe_value(a.to),
                                          safe_value(state, None)), flush=True)
    seen, dirty = None, False
    while True:
        try:
            if seen is None:
                os.makedirs(directory, exist_ok=True)
            names = {n for n in os.listdir(directory) if n.endswith(".md")}
            if seen is None:
                seen = load_state(state, set() if a.all else names)
                dirty = True
            if seen - names:
                seen.intersection_update(names)
                dirty = True
            if dirty:
                save_state(state, seen)
                dirty = False
            for name in sorted(names - seen):
                h = header(os.path.join(directory, name))
                if h is None:
                    h = {k: "?" for k in ("from", "to", "subject")}
                    report = True
                else:
                    report = not a.to or h.get("to") in (a.to, "all")
                if report:
                    print("NEW %s from=%s to=%s subject=%s" % (
                        safe_value(name, None), safe_value(h["from"]), safe_value(h["to"]),
                        safe_value(h["subject"])), flush=True)
                seen.add(name)
                dirty = True
                save_state(state, seen)
                dirty = False
        except OSError:
            # Preserve in-memory progress until the directory and state file are writable again.
            pass
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

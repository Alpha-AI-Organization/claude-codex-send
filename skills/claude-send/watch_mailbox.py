"""Print one line for every new message in a mailbox directory (for Claude Code's Monitor tool).

  python -u watch_mailbox.py [--dir DIR] [--to NAME] [--interval 2]

Messages already in the directory when it starts are ignored. Each new *.md file produces one line:
  NEW <filename> from=<sender> to=<recipient> subject=<subject>
Only the header is printed; open the file to read the body. Standard library only.
"""
import argparse, os, sys, time


def header(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return out
            for line in f:
                if line.strip() == "---":
                    break
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("AGENT_MAILBOX") or ".agent-mailbox")
    ap.add_argument("--to", default="", help="only report messages addressed to this name (or 'all')")
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()

    os.makedirs(a.dir, exist_ok=True)
    seen = {n for n in os.listdir(a.dir)}
    while True:
        for name in sorted(os.listdir(a.dir)):
            if name in seen or not name.endswith(".md"):
                continue
            seen.add(name)
            h = header(os.path.join(a.dir, name))
            if a.to and h.get("to") not in (a.to, "all"):
                continue
            print("NEW %s from=%s to=%s subject=%s" % (name, h.get("from", "?"), h.get("to", "?"), h.get("subject", "")),
                  flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

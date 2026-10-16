"""Leave a message for Claude Code in a shared mailbox directory.

  python claude_post.py --from codex --to claude [--dir DIR] [--subject S] <text | @file.txt | ->

The mailbox directory defaults to $AGENT_MAILBOX, else ./.agent-mailbox. The message becomes one
Markdown file, written to a temporary name and renamed so a watcher never sees a half-written file.
Standard library only.
"""
import argparse, os, re, sys, time

MAX_BYTES = 64 * 1024


def clean(name):
    return re.sub(r"[^A-Za-z0-9_-]", "", name)[:32] or "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="sender", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--dir", default=os.environ.get("AGENT_MAILBOX") or ".agent-mailbox")
    ap.add_argument("--subject", default="")
    ap.add_argument("text", help="message text, @file to read a file, or - for stdin")
    a = ap.parse_args()

    if a.text == "-":
        body = sys.stdin.read()
    elif a.text.startswith("@"):
        body = open(a.text[1:], encoding="utf-8").read()
    else:
        body = a.text
    if len(body.encode("utf-8")) > MAX_BYTES:
        sys.exit("message is larger than %d bytes; write it to a file and send the path" % MAX_BYTES)

    sender, to = clean(a.sender), clean(a.to)
    subject = " ".join(a.subject.split())[:120]
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    os.makedirs(a.dir, exist_ok=True)
    final = os.path.join(a.dir, "%s-%s-to-%s.md" % (stamp, sender, to))
    n = 1
    while os.path.exists(final):
        n += 1
        final = os.path.join(a.dir, "%s-%s-to-%s-%d.md" % (stamp, sender, to, n))
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("---\nfrom: %s\nto: %s\nsubject: %s\nsent: %s\n---\n\n%s\n" % (
            sender, to, subject, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), body.rstrip()))
    os.replace(tmp, final)
    print(final)


if __name__ == "__main__":
    main()

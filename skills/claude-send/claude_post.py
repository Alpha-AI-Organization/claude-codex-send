"""Leave a message for Claude Code in a shared mailbox directory.

  python claude_post.py --from codex --to claude [--dir DIR] [--subject S] <text | @file.txt | ->

Set the mailbox directory with --dir or AGENT_MAILBOX. The message becomes one Markdown file,
published only after writing is complete. The final absolute path is printed on stdout.
Standard library only.
"""
import argparse, os, re, secrets, sys, tempfile
from datetime import datetime, timezone

MAX_BYTES = 64 * 1024


def clean(name):
    return re.sub(r"[^A-Za-z0-9_-]", "", name)[:32] or "unknown"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="sender", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--dir", default=os.environ.get("AGENT_MAILBOX"))
    ap.add_argument("--subject", default="")
    ap.add_argument("text", help="message text, @file to read a file, or - for stdin")
    a = ap.parse_args(argv)
    if not a.dir:
        ap.exit(2, "error: set the mailbox directory with --dir or AGENT_MAILBOX\n")

    tmp = None
    try:
        if a.text == "-":
            body = sys.stdin.buffer.read().decode("utf-8") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        elif a.text.startswith("@"):
            with open(a.text[1:], encoding="utf-8-sig") as f:
                body = f.read()
        else:
            body = a.text
        if len(body.encode("utf-8")) > MAX_BYTES:
            ap.exit(2, "error: message is larger than %d bytes; write it to a file and send the path\n" % MAX_BYTES)

        directory = os.path.realpath(os.path.expanduser(a.dir))
        os.makedirs(directory, exist_ok=True)
        sender, to = clean(a.sender), clean(a.to)
        subject = " ".join(a.subject.split())[:120]
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S_%fZ")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=directory,
                                         prefix=".post-", suffix=".tmp", delete=False) as f:
            tmp = f.name
            f.write("---\nfrom: %s\nto: %s\nsubject: %s\nsent: %s\n---\n\n%s\n" % (
                sender, to, subject, now.strftime("%Y-%m-%dT%H:%M:%SZ"), body.rstrip()))
        final = os.path.join(directory, "%s-%s-to-%s-%x%s.md" % (
            stamp, sender, to, os.getpid(), secrets.token_hex(2)))
        os.replace(tmp, final)
        print(final)
        return 0
    except (OSError, UnicodeError, ValueError) as e:
        print("error: " + " ".join(str(e).splitlines()), file=sys.stderr)
        return 1
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    sys.exit(main())

"""Install local agent skills from the skills directory next to this script.

  python install.py [--only claude|codex] [--dry-run] [--force] [--link] [--uninstall]

Claude Code: codex-send, claude-receive -> <CLAUDE_CONFIG_DIR or ~/.claude>/skills/
Codex: claude-send -> <CODEX_HOME or ~/.codex>/skills/

Exit statuses: 0 = success; 1 = error; 2 = usage error; 3 = conflict without --force.
"""
import argparse, os, shutil, stat, sys
from pathlib import Path

TOOLS = {"claude": ("CLAUDE_CONFIG_DIR", ".claude", ("codex-send", "claude-receive")),
         "codex": ("CODEX_HOME", ".codex", ("claude-send",))}
NAMES = {name for _, _, names in TOOLS.values() for name in names}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                             getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def files(root):
    if linked(root) or not root.is_dir():
        return None
    found, pending = {}, [root]
    while pending:
        for path in pending.pop().iterdir():
            if path.name in IGNORE(str(path.parent), [path.name]):
                continue
            if linked(path):
                return None
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                found[path.relative_to(root)] = path.read_bytes()
            else:
                return None
    return found


def links_to(dest, source):
    target = os.readlink(dest)
    if os.name == "nt":
        if target.startswith("\\\\?\\UNC\\"):
            target = "\\\\" + target[8:]
        elif target.startswith("\\\\?\\"):
            target = target[4:]
    return os.path.normcase(os.path.abspath(dest.parent / target)) == os.path.normcase(str(source))


def check_parent(dest, directory):
    if dest.name not in NAMES or dest.parent.resolve() != directory:
        raise ValueError("destination is outside the intended skills directory: %s" % dest)


def remove(dest, directory):
    check_parent(dest, directory)
    if dest.is_symlink():
        dest.unlink()
    elif linked(dest):
        dest.rmdir()
    elif dest.is_dir():
        shutil.rmtree(dest)
    else:
        dest.unlink()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=tuple(TOOLS), help="select one tool and create its home if needed")
    ap.add_argument("--dry-run", action="store_true", help="print the plan without writing anything")
    ap.add_argument("--force", action="store_true", help="replace conflicting skill folders")
    ap.add_argument("--link", action="store_true", help="install directory symlinks instead of copies")
    ap.add_argument("--uninstall", action="store_true", help="remove only this repository's skill folders")
    a = ap.parse_args(argv)
    selected = (a.only,) if a.only else tuple(TOOLS)
    directories = {}
    try:
        homes = {tool: Path(os.environ.get(TOOLS[tool][0]) or str(Path.home() / TOOLS[tool][1])).expanduser().absolute()
                 for tool in selected}
        directories = {tool: home / "skills" for tool, home in homes.items()}
        base = Path(__file__).resolve().parent / "skills"
        if linked(base):
            raise ValueError("source skills directory must not be a link: %s" % base)
        sources = {}
        for tool in selected:
            for name in TOOLS[tool][2]:
                source = base / name
                if not source.is_dir() or linked(source):
                    raise ValueError("source folder missing or not a regular directory: %s" % source)
                contents = files(source)
                if contents is None:
                    raise ValueError("source contains links or unsupported files: %s" % source)
                sources[name] = (source, contents)

        actions, conflicts = [], []
        for tool, home in homes.items():
            if linked(home) or linked(home / "skills"):
                raise ValueError("tool home and skills directory must not be links: %s" % home)
            if not home.exists() and not a.only:
                print("skipped %s: %s does not exist (use --only %s to create it)" % (tool, home, tool))
                continue
            if home.exists() and not home.is_dir():
                raise ValueError("tool home is not a directory: %s" % home)
            directory = directories[tool] = (home / "skills").resolve()
            if directory.exists() and not directory.is_dir():
                raise ValueError("skills path is not a directory: %s" % directory)
            for name in TOOLS[tool][2]:
                source, contents = sources[name]
                dest = directory / name
                check_parent(dest, directory)
                if any(dest == src or dest in src.parents or src in dest.parents for src, _ in sources.values()):
                    raise ValueError("destination overlaps a source folder: %s" % dest)
                exists = os.path.lexists(dest)
                if a.uninstall:
                    action = "remove" if exists else "absent"
                elif not exists:
                    action = "install"
                elif (a.link and dest.is_symlink() and links_to(dest, source)) or (
                        not a.link and files(dest) == contents):
                    action = "unchanged"
                else:
                    action = "replace"
                    if not a.force:
                        conflicts.append(dest)
                actions.append((action, source, dest, directory))
        if conflicts:
            for dest in conflicts:
                print("conflict %s: use --force to replace" % dest, file=sys.stderr)
            return 3

        for action, source, dest, directory in actions:
            if action in ("absent", "unchanged"):
                print("%s %s" % (action, dest))
                continue
            if a.dry_run:
                print("would %s %s" % (action, dest))
                continue
            check_parent(dest, directory)
            if action in ("remove", "replace"):
                remove(dest, directory)
            if action != "remove":
                directory.mkdir(parents=True, exist_ok=True)
                check_parent(dest, directory)
                if a.link:
                    try:
                        dest.symlink_to(source, target_is_directory=True)
                    except (OSError, NotImplementedError) as e:
                        raise OSError("cannot link %s: %s; run without --link" % (dest, e)) from e
                else:
                    shutil.copytree(source, dest, symlinks=True, ignore=IGNORE)
            print("%s %s" % ({"install": "installed", "replace": "replaced", "remove": "removed"}[action], dest))
        return 0
    except (OSError, ValueError, RuntimeError) as e:
        print("error: " + " ".join(str(e).split()), file=sys.stderr)
        return 1
    finally:
        for tool, directory in directories.items():
            print("%s skills: %s" % (tool, directory))


if __name__ == "__main__":
    sys.exit(main())

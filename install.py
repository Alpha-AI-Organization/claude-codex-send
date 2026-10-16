"""Install local agent skills from the skills directory next to this script.

  python install.py [--only claude|codex] [--dry-run] [--force] [--link] [--uninstall]

Claude Code: codex-send, claude-receive -> <CLAUDE_CONFIG_DIR or ~/.claude>/skills/
Codex: claude-send -> <CODEX_HOME or ~/.codex>/skills/

Exit statuses: 0 = success; 1 = error; 2 = usage error; 3 = conflict without --force.
"""
import argparse, filecmp, os, shutil, stat, sys
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


def same(dest, source):
    if linked(dest) or not dest.is_dir():
        return False
    cmp = filecmp.dircmp(dest, source, ignore=filecmp.DEFAULT_IGNORES +
                        list(IGNORE("", os.listdir(dest) + os.listdir(source))))
    if any(linked(root / name) for root, names in ((dest, cmp.left_list), (source, cmp.right_list))
           for name in names):
        return False
    return not (cmp.left_only or cmp.right_only or cmp.common_funny or cmp.funny_files or
                any(filecmp.cmpfiles(dest, source, cmp.common_files, shallow=False)[1:])) and all(
        same(Path(sub.left), Path(sub.right)) for sub in cmp.subdirs.values())


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
        sources = {}
        for tool in selected:
            for name in TOOLS[tool][2]:
                source = (base / name).resolve()
                if not source.is_dir():
                    raise ValueError("source folder missing or not a regular directory: %s" % source)
                sources[name] = source

        actions, conflicts = [], []
        for tool, home in homes.items():
            if not home.exists() and not a.only:
                print("skipped %s: %s does not exist (use --only %s to create it)" % (tool, home, tool))
                continue
            directory = directories[tool] = (home / "skills").resolve()
            for name in TOOLS[tool][2]:
                source = sources[name]
                dest = directory / name
                if any(dest == src or dest in src.parents or src in dest.parents for src in sources.values()):
                    raise ValueError("destination overlaps a source folder: %s" % dest)
                exists = os.path.lexists(dest)
                if a.uninstall:
                    action = "remove" if exists else "absent"
                elif not exists:
                    action = "install"
                elif (a.link and dest.is_symlink() and links_to(dest, source)) or (
                        not a.link and same(dest, source)):
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
            if action in ("remove", "replace"):
                remove(dest, directory)
            if action != "remove":
                directory.mkdir(parents=True, exist_ok=True)
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

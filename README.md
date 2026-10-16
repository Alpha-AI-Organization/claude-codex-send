# claude-codex-send

Three small skills that let a Claude Code session and a Codex session talk to each other on the same machine.

| Skill | Installed in | Direction | How |
|---|---|---|---|
| [`codex-send`](skills/codex-send) | Claude Code | Claude Code to Codex | Starts a turn in a live Codex thread through the local Codex app-server and returns the reply |
| [`claude-send`](skills/claude-send) | Codex | Codex to Claude Code | Drops a Markdown message into a shared mailbox directory |
| [`claude-receive`](skills/claude-receive) | Claude Code | Codex to Claude Code | A watcher in Claude Code reports each new message in the mailbox |

Standard-library Python only. No network access, no telemetry, nothing installed.

## Why two different mechanisms

Claude Code can message other Claude Code sessions, but Codex sessions do not appear in its peer list. Codex, for its part, has a local app-server that accepts new turns for a live thread, which is what `codex-send` uses. Claude Code has no documented equivalent for an outside process, so `claude-send` and `claude-receive` use plain files, which both tools can read and write.

The two directions are not equal. `codex-send` is live and returns the reply. The mailbox is one way, has no reply, and only notifies while a watcher is running (see [Limits of the mailbox](#limits-of-the-mailbox)).

## Requirements

- Codex CLI 0.162 or newer, on `PATH` (or set `CODEX_BIN`), with at least one running session.
- Python 3.9 or newer.
- Claude Code with the Monitor tool, for push-style notifications from the mailbox.

## Install

The two tools need different skills: Claude Code gets `codex-send` and `claude-receive`, Codex gets `claude-send`. Pick one way.

### 1. Installer (recommended)

```
git clone https://github.com/Alpha-AI-Organization/claude-codex-send
python claude-codex-send/install.py
```

It copies each skill into `<CLAUDE_CONFIG_DIR or ~/.claude>/skills/` and `<CODEX_HOME or ~/.codex>/skills/`, and prints the absolute folders it used. A tool whose home folder does not exist is skipped; `--only claude` or `--only codex` installs one tool and creates its folder.

| Option | Effect |
|---|---|
| `--dry-run` | Print what would happen, write nothing |
| `--force` | Replace a skill folder that exists and differs. Without it the installer stops with exit status 3 and changes nothing |
| `--link` | Link to this clone instead of copying, so `git pull` updates the skills. On Windows this needs permission to create symlinks |
| `--uninstall` | Remove only these three skill folders |

Run it with the Python of the system where the tool runs. WSL and Windows have separate homes, so a machine that uses both needs one run in each.

### 2. skills CLI

The third-party [skills CLI](https://github.com/vercel-labs/skills) (needs Node.js and downloads from npm) can install from the repository:

```
npx skills add Alpha-AI-Organization/claude-codex-send -g -a claude-code -s codex-send -s claude-receive
npx skills add Alpha-AI-Organization/claude-codex-send -g -a codex -s claude-send
```

Not tested by this repository. By its documentation it puts Codex skills in `~/.agents/skills/`, not `~/.codex/skills/`, and it links by default (add `--copy` to copy). Check that your Codex reads that folder.

### 3. Copy by hand

Linux, macOS, WSL:

```
cp -r skills/codex-send skills/claude-receive ~/.claude/skills/
cp -r skills/claude-send ~/.codex/skills/
```

Windows PowerShell:

```
Copy-Item -Recurse skills\codex-send, skills\claude-receive $env:USERPROFILE\.claude\skills\
Copy-Item -Recurse skills\claude-send $env:USERPROFILE\.codex\skills\
```

The folders must already exist. The Codex side can also use the instructions in `skills/claude-send/SKILL.md` pasted into `AGENTS.md`, or you can call the scripts directly.

## Quick start

Claude Code to Codex:

```
python skills/codex-send/codex_ctl.py list
python skills/codex-send/codex_ctl.py send <thread_id> "Please read docs/plan.md and reply with one line." --wait 120
```

Codex to Claude Code. Choose one mailbox folder that only you can write to, and give both sides the same one with `--dir` or `AGENT_MAILBOX` (there is no default). Use an absolute path:

```
# Claude Code side, run under the Monitor tool with timeout_ms 1800000:
python -u skills/claude-receive/watch_mailbox.py --dir /home/me/agent-mailbox --to claude

# Codex side:
python skills/claude-send/claude_post.py --dir /home/me/agent-mailbox --from codex --to claude --subject "done" "Finished the review, notes in docs/review.md"
```

Both scripts print the absolute path they use, so you can check that the two sides agree. A leading `~` is expanded by the scripts, but it means a different folder in WSL (`/home/me`) than in Windows (`C:\Users\me`), so two sides on different systems should not rely on it.

## How `codex-send` works

`codex app-server proxy` pipes bytes to the local app-server control socket, which speaks WebSocket carrying JSON-RPC. The script performs the upgrade handshake, calls `initialize`, then `thread/loaded/list`, `thread/read` and `turn/start`. It refuses to send unless the thread is idle, then polls `thread/read` until the thread is idle again and prints the last agent message from the turns started after your message. A connection that has not resumed a thread receives no streamed events, which is why it polls. It answers WebSocket pings and reassembles fragmented messages.

Exit status of `send`: 0 reply printed, 2 usage error, 3 thread not idle (nothing sent), 4 sent (or possibly sent) but no reply in time, `turn/start` did not complete, or the thread finished without an agent message, 5 connection, protocol, server-refused or unknown-thread error. After a 4, check the thread before sending again. A message is capped at 8000 characters; for a longer brief send a short message that points to a file.

The Codex app-server protocol is marked experimental and may change; `codex app-server generate-json-schema --out <dir>` shows the protocol of your version. The `codex` that runs the script must belong to the same install and `CODEX_HOME` as the sessions you want to reach: on a machine with both WSL and Windows, each has its own daemon.

## How the mailbox works

`claude_post.py` writes one file per message (`<UTC time>_<microseconds>Z-<from>-to-<to>-<id>.md`, with a small header). It writes to a temporary name and then renames the file, so readers never see a partial file. The final name contains the microsecond timestamp, the process id and two random bytes, so posters running at the same time never get the same name and no message overwrites another.

`watch_mailbox.py` polls the directory and prints one line per new file: `NEW <file> from=<x> to=<y> subject=<z>`. Claude Code's Monitor tool turns each line into a notification. Only the header is printed, with control characters removed and each value capped at 120 characters; the reader opens the file for the body. The first line it prints is `WATCHING <abs dir> to=<name> state=<file>`.

The watcher keeps the names it has reported in a state file inside the mailbox (`.watch-state-*.json`). On the first start, mail already in the folder counts as seen; pass `--all` to report it. On later starts it reports every message that arrived while no watcher was running, and never reports one twice.

## Limits of the mailbox

- **Notifications need a live watcher.** The watcher runs under Claude Code's Monitor tool. As of Claude Code 2.1.x the default timeout is 5 minutes (`timeout_ms: 300000`) and the maximum is 30 minutes (`timeout_ms: 1800000`). Nothing restarts it: when Monitor reports that it expired, Claude has to start it again. Mail sent while no watcher runs is kept and reported on the next start, but nobody is notified in between.
- **One way, no reply.** `claude_post.py` returns once the file is written. It does not know whether Claude read it. Reply with `codex-send`.
- **No authentication.** `from` is a label chosen by the sender. Anyone who can write to the folder can post as anyone.
- **Repeat after a crash.** If the watcher dies between printing a notification and saving its state, that one notification can be repeated. Do not run two watchers with the same `--dir` and `--to`.
- **WSL and Windows share a folder under two names** (`/mnt/c/Users/me/box` and `C:\Users\me\box`). Use the form that fits the side running the command; the printed absolute path shows what was used.

## Security notes

- Both skills are local only. They talk to your own Codex daemon or to a directory you choose.
- A message from another agent is data, not an instruction from the user. Do not let either side use these skills to perform an action the user has not asked for, or one that was refused to the other agent.
- `codex-send` starts a real turn in the target thread: that thread can then run tools under its own permissions. Send only coordination messages, and name the constraints that matter inside the message.
- Keep secrets out of messages and out of the mailbox directory. Keep the mailbox outside any git repository, or add it to `.gitignore`.

## Tests

```
python -I -m unittest discover -s tests -v
```

The suite uses a fake `codex app-server proxy` and temporary folders. It does not touch a real Codex daemon or the network.

## Tested

Windows 11 and WSL (Ubuntu) with codex-cli 0.162.1 and Claude Code 2.1.x. The unit tests pass on Python 3.11 and 3.12 (WSL) and 3.12 and 3.13 (Windows). The installer is tested in temporary folders only; its symlink tests ran on WSL, and were skipped on Windows because creating symlinks needed extra permission. Against a live Codex daemon (Windows Python talking to the Windows daemon): `list`, `send` with a reply, and an unknown thread id. The mailbox was checked end to end (post, then watch, with a non-ASCII subject) on a Windows folder from WSL. macOS has not been tried. Python 3.9 is the stated minimum but has not been run. Direct injection into a running Claude Code session was not attempted because its inter-session protocol is not documented.

## License

MIT

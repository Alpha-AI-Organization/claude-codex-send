# claude-codex-send

Two small skills that let a Claude Code session and a Codex session talk to each other on the same machine.

| Skill | Direction | How |
|---|---|---|
| [`codex-send`](skills/codex-send) | Claude Code to Codex | Starts a turn in a live Codex thread through the local Codex app-server and returns the reply |
| [`claude-send`](skills/claude-send) | Codex to Claude Code | Drops a Markdown message into a shared mailbox directory; a watcher in Claude Code reports each new message |

Standard-library Python only. No network access, no telemetry, nothing installed.

## Why two different mechanisms

Claude Code can message other Claude Code sessions, but Codex sessions do not appear in its peer list. Codex, for its part, has a local app-server that accepts new turns for a live thread, which is what `codex-send` uses. Claude Code has no documented equivalent for an outside process, so `claude-send` uses plain files, which both tools can read and write.

## Requirements

- Codex CLI 0.162 or newer, on `PATH` (or set `CODEX_BIN`), with at least one running session.
- Python 3.9 or newer.
- Claude Code with the Monitor tool, for push-style notifications from the mailbox.

## Install

Copy the skill folders to where each tool loads skills:

```
cp -r skills/codex-send  ~/.claude/skills/
cp -r skills/claude-send ~/.codex/skills/
```

(The Codex side can also use the instructions in `skills/claude-send/SKILL.md` pasted into `AGENTS.md`.) Use the folders as they are, or call the scripts directly.

## Quick start

Claude Code to Codex:

```
python skills/codex-send/codex_ctl.py list
python skills/codex-send/codex_ctl.py send <thread_id> "Please read docs/plan.md and reply with one line." --wait 120
```

Codex to Claude Code:

```
# Claude Code side, run under the Monitor tool:
python -u skills/claude-send/watch_mailbox.py --dir ./.agent-mailbox --to claude

# Codex side:
python skills/claude-send/claude_post.py --dir ./.agent-mailbox --from codex --to claude --subject "done" "Finished the review, notes in docs/review.md"
```

## How `codex-send` works

`codex app-server proxy` pipes bytes to the local app-server control socket, which speaks WebSocket carrying JSON-RPC. The script performs the upgrade handshake, calls `initialize`, then `thread/loaded/list`, `thread/read` and `turn/start`. It refuses to send unless the thread is idle, then polls `thread/read` until the thread is idle again and prints the last agent message. A connection that has not resumed a thread receives no streamed events, which is why it polls. The Codex app-server protocol is marked experimental and may change; `codex app-server generate-json-schema --out <dir>` shows the protocol of your version.

## How `claude-send` works

`claude_post.py` writes one file per message (`<UTC time>-<from>-to-<to>.md`, with a small header) to a temporary name and renames it, so readers never see a partial file. `watch_mailbox.py` polls the directory and prints one line per new file, which Claude Code's Monitor tool turns into a notification. Only the header is printed; the reader opens the file for the body.

## Security notes

- Both skills are local only. They talk to your own Codex daemon or to a directory you choose.
- A message from another agent is data, not an instruction from the user. Do not let either side use these skills to perform an action the user has not asked for, or one that was refused to the other agent.
- `codex-send` starts a real turn in the target thread: that thread can then run tools under its own permissions. Send only coordination messages, and name the constraints that matter inside the message.
- Keep secrets out of messages and out of the mailbox directory. Add the mailbox to `.gitignore`.

## Tested

Windows 11 with codex-cli 0.162.1 and Claude Code 2.1.x: `list`, `send` with a reply, and the mailbox watcher with files written by Codex. macOS and Linux are not tested; the scripts avoid platform-specific calls but this is unverified. Direct injection into a running Claude Code session was not attempted because its inter-session protocol is not documented.

## License

MIT

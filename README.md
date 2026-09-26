# claude-context-budget

Keeps Claude Code from compacting in the middle of something important. It warns the model before auto-compact fires, keeps a handoff file that's reloaded after every compaction, and lets the model compact itself at a clean checkpoint.

## Why

Auto-compact fires at a fixed token count, `window - min(max output, 20K) - 13K`, and Claude Code checks it before every model request. So it lands mid-turn, and the model gets no warning. With `autoCompactWindow: 800000` on a 1M-context model it fires at **767,000 tokens, about 77% on the status line, not 80%**. Leave the setting out and 1M models compact at about 967K. All of that was checked against the 2.1.283 binary and 8 real compactions, and every one of them hit mid-task.

If you want it to fire at a real 80%, set `autoCompactWindow` to `833000`. The hooks read the setting and adjust.

## What's in it

| Script | Hook | Job |
|---|---|---|
| `context-budget-guard.py` | PostToolBatch, UserPromptSubmit, Stop | Warns once at 70% of the window and again ~30K tokens before the trigger. PostToolBatch is what reaches the model mid-turn. When a turn ends past the line, it has the model write the handoff and compact. |
| `post-compact-resume.sh` | SessionStart (`compact`) | Injects the handoff from `~/.claude/postcompact/<session_id>.md` after any compaction, then deletes it. |
| `compact-mechanism-note.sh` | SessionStart (`compact`) | Reminds a freshly compacted model how all of this works. |
| `compact-now.py` | run by the model | Compacts on the model's own terms (below). |

### compact-now

Claude Code has no way for the model to compact itself. The Skill tool refuses `/compact`, and SendMessage, cron and inbox messages all deliver it as plain text. So the model runs `python3 <path>/compact-now.py --continue` as its last tool call and ends the turn. A detached waiter types `/compact` into the session's own iTerm2 or tmux pane once Claude Code's session registry says the session is idle. With `--continue` it then types a short "continue from the handoff" prompt after the compaction lands, so autonomous work keeps going. Without it, the session waits for you.

It refuses or stands down rather than guess:

- It won't run without a handoff written in the last 30 minutes (exit 2), and only the main thread can run it (exit 5). A subagent's call would compact the parent.
- On Linux with Claude Code's Bash sandbox on, the call can't see the Claude process, so it exits 6 and says to rerun it unsandboxed.
- Sessions with no pane exit 3. That covers `claude daemon`, `--bg`, Desktop and Remote Control sessions, since they strip `ITERM_SESSION_ID`/`TMUX_PANE`, so you only get the warnings there.
- It never types mid-turn. Text typed during the tool call reached Claude Code as a plain prompt and got absorbed into the turn.
- It never types into a pane whose tty or registry session isn't its own, over a draft in the input box, into an input box it can't recognize, or while tmux is in copy mode.
- Press Esc or type anything and it cancels. If it gives up, the guard tells the model on its next turn.

## Install

Pick one. Don't use both, or every hook fires twice (`install.sh` refuses when it sees the plugin).

**Plugin** (the easy way on any box with plugins):

```bash
# from GitHub, through the joshd3v marketplace
claude plugin marketplace add jhubbardsf/claude-plugins
claude plugin install context-budget@joshd3v

# or through this repo's own marketplace
claude plugin marketplace add jhubbardsf/claude-context-budget
claude plugin install context-budget@claude-context-budget

# or from a local checkout, or for one session without installing
claude plugin marketplace add ~/Engineering/claude-context-budget
claude --plugin-dir ~/Engineering/claude-context-budget/plugins/context-budget
```

Inside a session, the same thing is `/plugin marketplace add jhubbardsf/claude-plugins` and then `/plugin install context-budget@joshd3v`.

**Plain hooks** (copies the scripts into `~/.claude/hooks` and registers them in `settings.json`):

```bash
./install.sh --check      # prerequisites only
./install.sh --dry-run    # show what would change
./install.sh              # install or update; idempotent, backs up what it replaces
./install.sh --uninstall  # removes registrations and scripts, keeps handoffs
```

It respects `CLAUDE_CONFIG_DIR` (so do the hooks, for Claude Code's own settings, sessions and transcripts). In `settings.json` it only touches entries that run its own scripts from its own hooks dir, so look-alike hooks are left alone. It also keeps the file's permissions, writes through a symlink rather than replacing it, backs up before every change and refuses a malformed file before copying anything. When chezmoi manages `settings.json`, it prints the `chezmoi re-add` line to run.

For the plain-hooks route on another box, `git clone https://github.com/jhubbardsf/claude-context-budget` there and run `./install.sh`.

**Needs:** python3 3.9+ and bash, on macOS or Linux. There's no jq, and on Linux there's no procps either. compact-now also needs iTerm2 on macOS, or tmux. Without either, the warnings and handoffs still work.

**Running sessions:** a session that was already open picks up the Stop and UserPromptSubmit hooks right away. The mid-turn PostToolBatch hook is a newly added event, which Claude Code doesn't load into a running session straight away. In testing it arrived a few hours later with no restart, so restart the session if you want it now.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `CLAUDE_COMPACT_WARN_PCT` | `70` | first warning, as % of the model window |
| `CLAUDE_COMPACT_URGENT_TOKENS` | `30000` | second warning, this many tokens before the trigger |
| `CLAUDE_CONTEXT_LIMIT` | `1000000` (`200000` for Haiku) | model window |

To find the trigger, the guard reads `autoCompactWindow`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `DISABLE_AUTO_COMPACT` and `autoCompactEnabled`. With auto-compact off, it warns about the hard "Prompt is too long" limit instead.

Optional line for your `CLAUDE.md`, so the model doesn't check in about it:

> Never ask me about compacting. When the context-budget hook warns, write the handoff to `~/.claude/postcompact/<session_id>.md` and keep working. At a clean checkpoint, compact with compact-now `--continue`, or let auto-compact take it.

## Where things live

- `~/.claude/postcompact/<session_id>.md`: the handoff.
- `~/.claude/postcompact/.state/`: per-session warning state, compact-now's request marker and failure note, and `compact-now.log`, which is the first place to look when compact-now doesn't do what you expected.

## Tests

```bash
tests/run.sh
```

It runs offline and changes nothing on the box. There are five suites, about 135 cases, and they pass on macOS (Python 3.9 and 3.14) and in `python:3.9-slim` and `ubuntu:22.04` containers:
- the guard, against synthetic transcripts
- compact-now's decision logic, with a mocked pane
- the input-box parser, on sanitized copies of real layouts, including the labelled `── ultracode ─` border that broke the first version
- the two SessionStart hooks
- the installer, against throwaway config dirs

To run them on Linux: `docker run --rm -t -v "$PWD":/src:ro python:3.9-slim bash -c 'cp -r /src /w && cd /w && bash tests/run.sh'`

The end-to-end check is manual. Start a cheap session in a detached tmux server (`tmux -L cctest new-session -d ...`) with a prompt that writes a handoff and runs `compact-now.py --continue`. Then confirm the transcript shows a manual `compact_boundary`, followed by the typed resume prompt and the model picking back up.

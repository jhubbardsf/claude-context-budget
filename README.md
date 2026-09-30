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

Claude Code has no model-callable compaction command. The Skill tool refuses `/compact`, and SendMessage, cron and inbox messages all deliver it as plain text. The model runs `python3 <path>/compact-now.py --continue` as its last tool call and ends the turn. A detached waiter types `/compact` into the session's own iTerm2 or tmux pane once Claude Code's session registry says the session is idle. With `--continue` it then types a short "continue from the handoff" prompt after the compaction lands, so autonomous work keeps going. Without it, the session waits for user input.

Native background sessions (`/bg`, `claude --bg`, or agent view) use a temporary `claude attach <jobId>` client in a private, detached tmux terminal. The helper checks the full session ID, worker PID, process start, and job ID, waits for an idle empty prompt, and checks a new manual `compact_boundary` before continuing. `Ctrl+Z` detaches the temporary client after completion; the background worker stays running. Failed attempts disconnect the client without sending cancellation keys. A single waiter owns this sequence, while the SessionStart hook continues to reload the handoff.

It refuses or stands down rather than guess:

- It won't run without a handoff written in the last 30 minutes (exit 2), and only the main thread can run it (exit 5). A subagent's call would compact the parent.
- On Linux with Claude Code's Bash sandbox on, the call can't see the Claude process, so it exits 6 and says to rerun it unsandboxed.
- Native background workers require `claude` and `tmux` on PATH. Other sessions without a supported pane or a matching registry exit 3; warnings and handoffs still work. A Desktop, Remote Control, or `-p` session is not assumed to be a native background worker.
- It never types mid-turn. Text typed during the tool call reached Claude Code as a plain prompt and got absorbed into the turn.
- It never types into a pane whose tty or registry session isn't its own, over a draft in the input box, into an input box it can't recognize, or while tmux is in copy mode.
- Press Esc or type anything and it cancels. If it gives up, the guard tells the model on its next turn.

### self-command

`self-command.py` reuses compact-now's transport to type a short allowlist of other slash commands (`/remote-control`, `/rename`, `/color`) and proves each one landed from the transcript or the session registry. `/compact` stays with compact-now.

```bash
python3 <path>/self-command.py rename "TB3 - Task58 - submitted"   # a session renames itself after its turn
python3 <path>/self-command.py color green
python3 <path>/self-command.py --pid 12345 --wait rc              # drive another live session now
python3 <path>/self-command.py rc-sweep --dry-run                 # which sessions lost Remote Control
python3 <path>/self-command.py install-watch                      # macOS LaunchAgent running rc-watch
python3 <path>/self-command.py status
```

**Remote Control after an account switch.** When the signed-in claude.ai account changes (cc-account switching accounts, or `/login`), every session with Remote Control on drops it and prints "Remote Control disconnected — signed-in claude.ai account or organization changed on this machine". `rc-watch` scans the live sessions every 15 seconds. For each one whose newest Remote Control event is that notice, it waits until the session is idle with an empty input box, then types `/remote-control`, which reconnects under the new account. It confirms the new `/remote-control is active` record before counting it as done. It doesn't need a hook in the switcher, so it covers any cause of the account change.

Guardrails, beyond compact-now's:

- A command runs only if it's on the allowlist and its argument validates. A separate deny list (`/clear`, `/exit`, `/login`, `/model` and others) can't be overridden, and an argument can't contain a newline.
- `/remote-control` is refused while the session's link is still up (the registry has a `bridgeSessionId`), because on a connected session it opens a Disconnect / Show QR / Continue picker instead. If any command does leave a picker open (status `waiting`, an "Esc to" footer, and no permission prompt), it presses Esc once.
- One command per session at a time, enforced with a kernel `flock` on `<sid>.self-command.lock` that compact-now also honours. A pending compact-now request always wins: self-command stands down and compact-now waits out any delivery already in flight, so the two never type into one box.
- Each command must be confirmed by a record written after the keystrokes (a new `custom-title`, `agent-color` or `bridge_status`), so a command that was swallowed is never logged as done. A command that's already in effect is skipped.
- The watcher types `/remote-control` at most twice per disconnect, with a 5-minute gap. It gives up after six transport failures (no pane, pane unreachable). Waiting on a busy session or a draft never counts as a failure, it just backs off and tries again. It also respects the 4-per-hour cap.
- It checks the registry's `procStart` against the live process, so a crashed session whose pid got reused is never typed into.
- A running watcher restarts itself when its scripts change. It exits cleanly if they're removed, and `install.sh --uninstall` takes the LaunchAgent down with them.
- Dim ghost text in the box (a brand-new session's `❯ Try "..."` placeholder, or a prompt suggestion) counts as empty. tmux and background panes are read with styling (`capture-pane -e`). iTerm gives plain text, so only the `Try "..."` shape is recognized there. A real draft still blocks typing.

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

**Needs:** python3 3.9+ and bash, on macOS or Linux. There's no jq, and on Linux there's no procps either. Interactive compact-now needs iTerm2 on macOS, or tmux. Native background compact-now needs both tmux and a Claude CLI with `agents --json` and `attach` support (developed against 2.1.284). Without a supported transport, warnings and handoffs still work.

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

The seven suites run offline against throwaway state. When tmux is installed, the background transport test starts an isolated tmux server running a fake Claude CLI, verifies command delivery and detach, then removes the server. No real Claude session is opened:
- the guard, against synthetic transcripts
- compact-now's decision logic, with a mocked pane
- native background identity, continuation ownership, and the temporary attach transport
- the input-box parser, on sanitized copies of real layouts, including the labelled `── ultracode ─` border that broke the first version
- the two SessionStart hooks
- the installer, against throwaway config dirs
- self-command's allowlist, argument checks, Remote Control detection, sweep candidates, attempt backoff, locks and the placeholder rule, against synthetic transcripts and registries

To run them on Linux: `docker run --rm -t -v "$PWD":/src:ro python:3.9-slim bash -c 'cp -r /src /w && cd /w && bash tests/run.sh'`

The end-to-end check is manual. Start a cheap session in a detached tmux server (`tmux -L cctest new-session -d ...`) with a prompt that writes a handoff and runs `compact-now.py --continue`. Then confirm the transcript shows a manual `compact_boundary`, followed by the typed resume prompt and the model picking back up.

For native background validation, fork a disposable session, background it, and ask it to write a fresh handoff and run `python3 ~/.claude/hooks/compact-now.py --continue` as its last tool call. The same worker must compact, continue, and remain available in agent view. Transport tests use a fake CLI; they do not establish end-to-end compatibility with Claude's live fullscreen prompt. Check `~/.claude/postcompact/.state/compact-now.log` for the attach, boundary, resume, and detach results.

self-command was checked by hand on 2026-09-30 (Claude Code 2.1.286) against throwaway sessions: `/color`, `/rename` and a `/remote-control` reconnect through a background attach, `/color` through an interactive tmux pane on a fresh session, and self mode (a session queued `/rename` from its own Bash call, held while a picker was open, then typed and verified it). Its log is `~/.claude/postcompact/.state/self-command.log`.

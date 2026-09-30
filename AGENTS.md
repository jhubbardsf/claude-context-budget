# AGENTS.md

Claude Code hooks that warn the model before auto-compact, keep a reloadable handoff, and let the model compact itself. README.md has the what and why. This file covers working on it.

## Layout

- `plugins/context-budget/` is the plugin: `.claude-plugin/plugin.json`, `hooks/hooks.json` (paths via `${CLAUDE_PLUGIN_ROOT}`), and `scripts/`, which holds the only copy of the code.
- `.claude-plugin/marketplace.json` makes the repo its own single-plugin marketplace.
- `install.sh` plus `installer/settings_merge.py` is the non-plugin install into `~/.claude/hooks`.
- `tests/run.sh` runs all six suites, offline (README has the Docker one-liner for Linux). The background suite uses a private tmux server with a fake Claude CLI when tmux is available. Add a regression case for every bug fixed, and run the suites on Linux before calling a change done.

## Rules

- **The scripts here are the source of truth.** The maintainer's Mac runs them as plain hooks installed by `./install.sh`, with the installed copies tracked in chezmoi. After changing a script: run the tests, run `./install.sh` (it backs up and replaces what's changed), then `chezmoi re-add` the four files in `~/.claude/hooks`. Don't edit `~/.claude/hooks` directly.
- Claude Code's own files (settings.json, .claude.json, sessions/, projects/) come from `CLAUDE_CONFIG_DIR` when it's set (`config_dir()`); our handoff/state dir stays at `~/.claude/postcompact` either way. cc-account follower sessions run with their own config dir.
- Scripts locate each other relative to their own directory. Never hardcode `~/.claude/hooks`, because the plugin cache path differs.
- Keep everything Python 3.9 compatible and free of jq and BSD-only tools, because the same bytes run on macOS (system python 3.9, bash 3.2) and Ubuntu (bash 5, GNU coreutils). Watch `~` in `${var/…}` substitutions: bash 3.2 and bash 5 disagree.
- Bump `version` in both `plugin.json` and `marketplace.json` for any change that should reach plugin installs, then run `claude plugin validate plugins/context-budget` and `claude plugin validate .`.
- 🛑 `claude plugin init` scaffolds into `~/.claude/skills/<name>/`, which auto-loads in every session, and **not** into the current directory. Don't use it here.

## Facts the code depends on (Claude Code 2.1.283, measured 2026-09-26)

- Trigger = `min(window, model window) - min(max output, 20000) - 13000`, checked at the top of every query-loop iteration, including between tool calls.
- Only `additionalContext` in the JSON `hookSpecificOutput` reaches the model from PostToolBatch/PostToolUse. Plain stdout doesn't. Stop `additionalContext` continues the turn (capped at 8 in a row).
- The model's running Bash `tool_use` is already in the main transcript while it executes. A subagent's calls go under `subagents/`, which is what the main-thread check relies on.
- `~/.claude/sessions/<pid>.json` has `status` (`busy`/`idle`), `statusUpdatedAt` (ms), `sessionId` and `procStart`.
- Typing `/compact` mid-turn gets it absorbed as a plain prompt. Typing it at an idle prompt runs it. A keystroke during "Compacting…" cancels it.
- Enter has to be its own write: the input tokenizer only splits a CR out of a read chunk shorter than 64 chars.
- The input box's top border can carry a label (`── ultracode ─`, a session name). The bottom border never does, so the parser anchors on the bottom.
- Real typed prompts carry `origin.kind: "human"`. Stop-hook feedback is `isMeta`, and the compact summary is `isCompactSummary`.
- Running sessions hot-reload changed commands on existing hook events right away. A newly added event (PostToolBatch here) isn't loaded immediately: one session only started running it hours later, without a restart.

## Native background compaction (2.1.284)

- A native background registry has `kind: bg`, `jobId`, full `sessionId`, worker `pid` and `procStart`. Background workers can inherit stale terminal variables, so registry kind takes precedence when choosing the transport.
- Native bg uses a private tmux server solely for a `claude attach <jobId>` client. The worker's daemon-owned PTY differs from the client's tmux PTY; each identity is checked independently. No second `--resume` process is started.
- The bg waiter owns attach through continuation and cleanup. `after_compact` leaves its marker in place so a separate hook process cannot race its terminal cleanup. The handoff hook still injects and deletes the handoff normally.
- Success detaches with Ctrl+Z; failure disconnects only the private attach client without sending keys into an unfinished compaction. Cleanup never stops the background worker or supervisor.
- Offline tests cover the transport with a fake CLI. A real forked/background Claude session is the required manual check for fullscreen prompt recognition and actual compaction.

## self-command (2026-09-30)

- `self-command.py` imports `compact-now.py` as a module (importlib, it has a hyphen) and reuses its transport, so changing a compact-now function signature breaks self-command too. `tests/test_self_command.py` loads it the same way.
- Remote Control facts (2.1.286): a disconnect is a `system`/`informational` record starting `Remote Control disconnected — ...`; the account-switch one says `signed-in claude.ai account or organization changed`. A reconnect is `system`/`bridge_status` `/remote-control is active · ...`. The registry carries `bridgeSessionId` only while connected. `/remote-control` on a connected session opens a Disconnect / Show QR / Continue picker, so it must be gated on the link being down.
- A brand-new session's box shows a dim `❯ Try "..."` placeholder that plain captures read as a draft; `box_empty` treats that exact shape as empty. A brand-new session may also have no transcript file yet, so verifiers look it up again each time.
- `docs/self-command-design.md` is the original sketch; `/model` and `/mcp` aren't built.

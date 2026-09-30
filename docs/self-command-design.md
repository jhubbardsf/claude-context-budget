# Design sketch: `self-command`, a general slash-command transport

Status: v1 implemented 2026-09-30 as `scripts/self-command.py` with `/remote-control`, `/rename` and `/color`, plus an `rc-watch` LaunchAgent that reconnects Remote Control after an account switch. It reuses compact-now's transport by importing it rather than moving it into a shared module (migration step 1 is still open). `/model` and `/mcp` aren't built. Written 2026-09-29 after background self-compaction shipped in 0.1.1.

## Problem

Claude Code gives the model no way to run its own slash commands. The Skill tool refuses them, and
SendMessage, cron and inbox deliver them as plain text. `compact-now.py` already solves this for one
command. It waits until the session is idle with an empty input box, types into that box, then proves
from the transcript that the command landed. It does this over two transports: the session's own
iTerm2 or tmux pane, and, for native background sessions, a private tmux server running
`claude attach <jobId>`.

Nothing in that transport is specific to `/compact`. Only the verification step and the resume prompt
are. `self-command` pulls the transport out so other commands can use it, and keeps `/compact` as one
command among several.

## Scope

In scope:

- A command registry: an allowlist with a per-command verifier, arguments policy and rate limit.
- One shared transport for delivery, identity checks and stand-down rules, lifted from `compact-now.py`.
- `compact-now.py` kept as a thin wrapper, so existing hooks, docs and muscle memory keep working.

Out of scope:

- Arbitrary text into the prompt. The helper never types anything that isn't a registered command.
- Anything that already has a CLI path. Forking is
  `claude --bg --resume <id> --fork-session --name <name>` from Bash, and needs no typing at all.

## Command registry

Each entry declares what may be typed, how success is proven, and how often it may run.

| command | args | verified by | rate limit | notes |
|---|---|---|---|---|
| `/compact` | none | a new manual `compact_boundary` in the transcript | 1 per 10 min | existing behavior, including `--continue` |
| `/rename <title>` | 1-80 printable chars, no newline | new `custom-title` event whose title matches | 3 per hour | drives iTerm tab names and FleetView |
| `/color <name>` | one of CC's palette: red blue green yellow purple orange pink cyan, or `default` | new `agent-color` event with that color | 6 per hour | `cc-tab-colors` repaints the tab within ~3 s |
| `/model <id>` | a model id from an explicit list | next assistant record's `model` field (to verify) | 4 per hour | step down for long polling, back up for writing |
| `/mcp` reconnect | server name from an allowlist | MCP tool reachable again (to verify) | 2 per 10 min | see open questions, since `/mcp` is a menu |

Permanently denied, even if someone adds them to the registry: `/clear`, `/exit`, `/quit`, `/logout`,
`/login`, `/permissions`, `/config`, `/resume`, `/init`, and anything that installs plugins or edits
settings. The deny list is checked separately from the allowlist, so a registry mistake can't enable them.

## Shared transport (lifted from `compact-now.py`)

These parts move into a `transport` module unchanged:

- Target selection: `pane_target` (iTerm/tmux) or `background_target` (native bg), chosen by the
  registry's `kind`, since bg workers can inherit stale terminal variables.
- Identity checks: `claude_tty`, `registry`, `background_worker_matches`, `session_state`.
- Prompt parsing: `is_rule` and `prompt_box_empty`, including the labelled top border fix from 0.1.1.
- Delivery: `pane` / `background_pane`, with Enter sent as its own write.
- Background lifecycle: `open_background_pane`, `close_background_pane`, detach with Ctrl+Z.
- Stand-down: `user_acted_since`, which cancels on Esc or on any human prompt after the request.
- Logging to `~/.claude/postcompact/.state/self-command.log`, one line per state change.

The waiter loop from `waiter` / `background_waiter` becomes generic:
`request -> wait for idle -> type -> verify -> (optional follow-up) -> clean up`. Each registry entry
supplies `verify(transcript, since) -> bool` and an optional `follow_up()`, which is where
`/compact --continue` keeps its resume prompt.

## Queueing

- One pending command per session. A second request while one is queued is refused with exit 4 and
  names the queued command. Queueing several would let two keystroke sequences interleave in the box.
- The marker moves from `<sid>.compact-requested` to `<sid>.self-command.json` with
  `{command, args, ts, requested_by}`. The wrapper keeps writing the old name for one release so the
  guard's `.compact-failed` relay keeps working.
- Rate limits live in `<sid>.self-command-history.json`, a rolling log of `{command, ts, result}`.
  An over-limit request is refused before anything is queued.

## CLI

```
self-command.py rename "TB3 - Task58_v2 - submitted"
self-command.py color green
self-command.py compact --continue          # same as compact-now.py --continue
self-command.py --dry-run color green       # prints target, transport and verifier; types nothing
self-command.py --status                    # queued command, recent history, last failure
```

Exit codes follow `compact-now.py`: 0 queued, 2 precondition failed (for `/compact`, no fresh
handoff), 3 no supported transport, 4 already queued or over the rate limit, 5 called from a
subagent, 6 command not allowed.

## Safety rules

1. Main thread only. A subagent's Bash would drive the parent session (exit 5, same check as today).
2. Type only into an idle session, into a recognized, empty input box. Never type into a draft, an
   unrecognized box, or tmux copy mode.
3. Stand down on any user input after the request. The user always wins the race.
4. Verify every command from the transcript or the tool surface. A keystroke that isn't confirmed is
   logged as a failure and relayed through the guard, never reported as success. The first bg attempt
   in 0.1.1 attached, found no box it recognized, and dropped silently. That's the bug this rule exists
   to catch.
5. Arguments are validated against the entry's schema, and never contain a newline, so one command
   can't smuggle a second one in.
6. Background attach is always cleaned up: detach on success, disconnect only the attach client on
   failure, and never stop the worker.

## Open questions to settle before building

- **`/mcp` reconnect.** `/mcp` opens an interactive menu. Driving it means arrow keys against a
  rendered list, which is more fragile than typing one command. Check whether 2.1.28x has a one-shot
  form. If it doesn't, keep reconnect out of v1.
- **`/model` verification.** Confirm the transcript records a model switch as an event, or fall back
  to the `model` field on the next assistant record.
- **Title and color events.** `custom-title` and `agent-color` are known from `cc-tab-colors`. Confirm
  they're written synchronously when the command runs, not on the next turn.
- **Reply prompts.** Some commands may print a confirmation that needs a keypress. Capture the screen
  after each command during testing and record any that do.

## Test plan

- Extend `tests/test_prompt_box.py` and `tests/test_bg_compact.py` so the fake CLI accepts any
  registered command and emits the matching transcript event.
- A table-driven test per registry entry covers accepted args, rejected args (newline, over length,
  unknown color), the verifier on synthetic transcripts, and the rate limiter.
- A deny-list test proves `/clear` and friends are refused even when injected into the registry.
- Manual end to end on a forked background session, the same as 0.1.1's validation: rename, color,
  then compact with `--continue`, checking `self-command.log` for attach, typed, verified and detach
  on each.

## Migration

1. Move the transport into `scripts/self_command/transport.py` with no behavior change. The existing
   suites must pass untouched.
2. Add the registry with `/compact` as the only entry, and turn `compact-now.py` into a wrapper.
3. Add `/rename` and `/color`, the two with known verifiers.
4. Add `/model`, and `/mcp` if it has a one-shot form, once the open questions are settled.
5. Bump the plugin version at each step, since installs only update on a version change.

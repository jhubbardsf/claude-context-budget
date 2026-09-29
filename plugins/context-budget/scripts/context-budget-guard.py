#!/usr/bin/env python3
"""Context-budget guard: warn the model before Claude Code auto-compacts.

Registered on three events, one script:
  PostToolBatch     between tool calls, i.e. MID-TURN. This is the one that matters:
                    auto-compact is checked before every model request, so it lands in
                    the middle of long agentic turns (all 8 in the TerminalBench project
                    did), and the model gets no native warning at all.
  UserPromptSubmit  a crossing that happened between turns.
  Stop              the turn is ending past the warning line: make sure the handoff is
                    written, and compact at this natural stopping point.

The warning tells the model to steer to a clean checkpoint, keep the handoff at
~/.claude/postcompact/<session_id>.md fresh (post-compact-resume.sh reloads it after
ANY compaction, auto included), and optionally compact itself with compact-now.py. It
also passes on a <sid>.compact-failed note when a queued compact-now gave up.

Where auto-compact fires (verified against the 2.1.283 binary and 8 real compactions):
  window  = CLAUDE_CODE_AUTO_COMPACT_WINDOW env > autoCompactWindow setting > model window
  trigger = min(window, model window) - min(max output, 20000) - 13000
so autoCompactWindow=800000 on a 1M model fires at 767,000 tokens (~77%), not 80%.

Context size = input + cache_read + cache_creation + output of the newest main-thread
assistant record with nonzero usage, stopping at the last compact_boundary. That's the
figure Claude Code's own counter starts from (so it can read a little above the status
line, which leaves output out). It still runs low by whatever tool results came back since,
which the urgent tier's gap absorbs.

Tunables (env): CLAUDE_COMPACT_WARN_PCT (70, % of the model window),
CLAUDE_COMPACT_URGENT_TOKENS (30000, tokens before the trigger),
CLAUDE_CONTEXT_LIMIT (model window, 1000000; 200000 for Haiku).

Fails open: any error exits 0 with no output. Python 3.9 compatible, since a session
with a stripped PATH can land on /usr/bin/python3.
"""
import json
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")
HANDOFF_DIR = os.path.join(HOME, ".claude", "postcompact")
STATE_DIR = os.path.join(HANDOFF_DIR, ".state")


def tilde(path):
    """Abbreviate $HOME to ~, but only on a real path boundary."""
    if HOME not in ("", "/") and path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


COMPACT_NOW = "python3 " + tilde(os.path.join(os.path.dirname(os.path.abspath(__file__)), "compact-now.py"))

OUTPUT_RESERVE = 20000       # min(model max output, 20000); every current model is >= 20K
AUTOCOMPACT_BUFFER = 13000
HARD_BLOCK_BUFFER = 3000     # "Prompt is too long" line when auto-compact is off
TAIL_CHUNK = 1 << 20
HANDOFF_MAX_AGE = 30 * 60    # compact-now.py refuses a handoff older than this
MARKER_FRESH = 45 * 60       # a queued compact-now counts for this long (its waiter's own limit)


def env_num(name, default):
    raw = os.environ.get(name, "").strip()
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def env_truthy(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def context_tokens(transcript):
    """(tokens, model) of the newest main-thread request, or (None, None).

    Mirrors Claude Code's own counter: walk back to the newest assistant record with
    nonzero input usage, skipping synthetic/unmetered/sidechain records, and stop at the
    last compact_boundary (everything before it is gone). Reads the file backwards in 1 MB
    chunks, so cost doesn't grow with transcript size.
    """
    try:
        size = os.path.getsize(transcript)
        f = open(transcript, "rb")
    except OSError:
        return None, None
    with f:
        pos, carry = size, b""
        while pos > 0:
            step = min(TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + carry).split(b"\n")
            carry = lines.pop(0) if pos > 0 else b""
            for line in reversed(lines):
                boundary = b'"compact_boundary"' in line
                if not boundary and b'"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                    return 0, None
                if rec.get("type") != "assistant" or rec.get("isSidechain") or rec.get("isUnmetered"):
                    continue
                msg = rec.get("message") or {}
                model = msg.get("model")
                if model == "<synthetic>":
                    continue
                usage = msg.get("usage") or {}
                inputs = sum(usage.get(k) or 0 for k in
                             ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if inputs > 0:
                    return inputs + (usage.get("output_tokens") or 0), model
    return None, None


def config_dir():
    """Claude Code keeps settings, sessions and transcripts here; CLAUDE_CONFIG_DIR moves them all.
    Our own handoff/state dir stays at ~/.claude/postcompact either way."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def setting(key, cwd):
    """First value of `key` across local > project > user settings."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    paths = []
    if root:
        paths += [os.path.join(root, ".claude", "settings.local.json"),
                  os.path.join(root, ".claude", "settings.json")]
    paths.append(os.path.join(config_dir(), "settings.json"))
    for path in paths:
        data = read_json(path)
        if isinstance(data, dict) and key in data:
            return data[key]
    return None


def model_window(model):
    default = 200000 if model and "haiku" in model else 1000000
    return int(env_num("CLAUDE_CONTEXT_LIMIT", default))


def auto_compact_off(cwd, check_global):
    if env_truthy("DISABLE_AUTO_COMPACT") or env_truthy("DISABLE_COMPACT"):
        return True
    val = setting("autoCompactEnabled", cwd)
    if val is not None:  # any settings file outranks the legacy ~/.claude.json value
        return val is False
    if check_global:  # pre-migration fallback; ~600 KB, so only read on the slow path
        legacy = (os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json") if os.environ.get("CLAUDE_CONFIG_DIR")
                  else os.path.join(HOME, ".claude.json"))
        cfg = read_json(legacy)
        return isinstance(cfg, dict) and cfg.get("autoCompactEnabled") is False
    return False


def compact_trigger(limit, cwd, check_global=False):
    """(tokens where auto-compact fires, auto_compact_on)."""
    if auto_compact_off(cwd, check_global):
        return limit - OUTPUT_RESERVE - HARD_BLOCK_BUFFER, False
    window = int(env_num("CLAUDE_CODE_AUTO_COMPACT_WINDOW", 0))
    if not window:
        try:
            window = int(setting("autoCompactWindow", cwd) or 0)
        except (TypeError, ValueError):
            window = 0
    window = min(window or limit, limit)
    trigger = window - OUTPUT_RESERVE - AUTOCOMPACT_BUFFER
    pct = env_num("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", 0)
    if 0 < pct <= 100:  # can only lower it, and it's a % of the reserve-adjusted window
        trigger = min(int((window - OUTPUT_RESERVE) * pct / 100), trigger)
    return trigger, True


def pane_available():
    """Whether compact-now.py has a candidate transport, without starting a process.

    Native background jobs need a temporary `claude attach` in an isolated tmux PTY.
    The helper validates the job/session identity before attaching; this hook only
    checks prerequisites. Background jobs can inherit stale terminal variables.
    """
    if os.environ.get("CLAUDE_JOB_DIR"):
        return bool(shutil.which("claude") and shutil.which("tmux"))
    return bool(os.environ.get("ITERM_SESSION_ID") or os.environ.get("TMUX_PANE"))


def fmt(n):
    return "{:,}".format(int(n))


def build_message(kind, used, limit, trigger, auto_on, handoff, handoff_state):
    """kind: warn | urgent | stop. handoff_state: fresh | stale | missing."""
    pct = used * 100.0 / limit
    left = max(trigger - used, 0)
    parts = ["[context-budget hook] Context is at {:.1f}% ({} of {} tokens).".format(
        pct, fmt(used), fmt(limit))]
    if auto_on:
        parts.append("Auto-compact fires by itself at about {} tokens, roughly {} from here, before the next "
                     "model request once it's crossed, so it can land in the middle of a task.".format(
                         fmt(trigger), fmt(left)))
    else:
        parts.append("Auto-compact is OFF, so nothing compacts this session by itself: at about {} tokens "
                     "(roughly {} from here) requests start failing with 'Prompt is too long' and the turn "
                     "stops mid-step.".format(fmt(trigger), fmt(left)))
    pane = pane_available()
    if os.environ.get("CLAUDE_JOB_DIR"):
        if pane:
            parts.append("For this native background session, the helper temporarily attaches through an "
                         "isolated tmux terminal, waits for an idle empty prompt, compacts, and detaches "
                         "while leaving the background session running.")
        else:
            parts.append("Native background self-compaction requires both claude and tmux on PATH for "
                         "the helper's temporary attach; those prerequisites are unavailable here.")
    how = ("make `{} --continue` your last tool call and end the turn. Compaction runs as soon as the turn "
           "ends, then the work picks back up from the handoff. Leave off --continue if the task is finished "
           "and you're waiting on the user.").format(COMPACT_NOW)
    write = ("write the handoff to {}: the task and goal, decisions made, what's done, the exact next action, "
             "files and paths in play, and gotchas".format(handoff))
    if kind == "warn":
        parts.append("Finish the step you're on and hold off on big new reads or long multi-step operations "
                     "until you've checkpointed.")
        parts.append("At the next clean checkpoint, {}. Overwrite it at every checkpoint after that, because "
                     "it's reloaded automatically after any compaction.".format(write))
        if pane:
            parts.append(("Once the handoff is written you can compact on your own terms: " if auto_on else
                          "Compacting before that line is required here, not optional: once the handoff is "
                          "written, ") + how)
        elif auto_on:
            parts.append("This session has no terminal pane or supported background attach transport, so it "
                         "can't compact itself; a current "
                         "handoff is what protects the work when auto-compact fires.")
        else:
            parts.append("This session can't compact itself either, so stop at a clean checkpoint before the "
                         "limit and tell the user it needs a /compact.")
    elif kind == "urgent":
        parts.append("That's one or two large tool results away. Before anything else, {}.".format(write))
        if pane:
            parts.append("Then compact at this checkpoint: " + how)
        elif auto_on:
            parts.append("Then keep the next steps small, so the automatic compaction lands on a fresh handoff.")
        else:
            parts.append("Then stop at this checkpoint and tell the user the session needs a /compact.")
    else:  # stop
        if handoff_state == "missing":
            parts.append("The handoff hasn't been written since context crossed the warning line, so {} now.".format(
                write))
        elif handoff_state == "stale":
            parts.append("The handoff at {} is over {} minutes old, so refresh it now.".format(
                handoff, HANDOFF_MAX_AGE // 60 - 3))
        if pane:
            parts.append("This is a natural stopping point, so compact here: run `{}` as the last step, adding "
                         "--continue only if there's unfinished autonomous work to resume. Then end the turn."
                         .format(COMPACT_NOW))
        elif not auto_on:
            parts.append("Then end the turn and tell the user the session needs a /compact.")
        else:
            parts.append("Then end the turn.")
    if auto_on or pane:
        parts.append("This is routine housekeeping, so don't ask the user about it.")
    return " ".join(parts)


def emit(event, text):
    json.dump({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, sys.stdout)


def save_state(path, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = "{}.{}".format(path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def pop_failure_note(sid):
    """compact-now.py leaves <sid>.compact-failed when a queued request gave up."""
    path = os.path.join(STATE_DIR, sid + ".compact-failed")
    note = read_json(path)
    if note is None:
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    reason = note.get("reason") if isinstance(note, dict) else None
    return ("[context-budget hook] compact-now.py could not confirm its queued request completed ({}). "
            "The command may already have been sent. Keep the handoff current, let any ongoing compaction "
            "finish, and check the session before retrying at a clean checkpoint. Auto-compact remains "
            "available if enabled.".format(reason or "it gave up"))


def compact_queued(sid, now):
    """True while a compact-now request from this session is still live."""
    path = os.path.join(STATE_DIR, sid + ".compact-requested")
    try:
        return now - os.path.getmtime(path) < MARKER_FRESH
    except OSError:
        return False


def handoff_state(handoff, since, now):
    """fresh | stale | missing, judged the way compact-now.py will judge it."""
    try:
        mtime = os.path.getmtime(handoff)
    except OSError:
        return "missing"
    if mtime < since - 60:
        return "missing"  # it predates this context cycle's warning
    return "fresh" if now - mtime < HANDOFF_MAX_AGE - 180 else "stale"


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name") or ""
    sid = data.get("session_id") or ""
    transcript = data.get("transcript_path") or ""
    if not sid or not transcript or data.get("agent_id"):
        return  # subagents have their own window; this guards the main thread only
    if event == "Stop" and data.get("stop_hook_active"):
        return
    notes = []
    failure = pop_failure_note(sid)
    if failure:
        notes.append(failure)

    used, model = context_tokens(transcript)
    message = decide(event, sid, data.get("cwd") or "", used, model) if used is not None else None
    if message:
        notes.append(message)
    if notes:
        emit(event, "\n\n".join(notes))


def decide(event, sid, cwd, used, model):
    limit = model_window(model)
    warn_pct = env_num("CLAUDE_COMPACT_WARN_PCT", 70.0)
    urgent_gap = int(env_num("CLAUDE_COMPACT_URGENT_TOKENS", 30000))
    state_path = os.path.join(STATE_DIR, sid + ".json")
    state = read_json(state_path) or {}

    def thresholds(check_global):
        trig, on = compact_trigger(limit, cwd, check_global)
        return trig, on, min(int(limit * warn_pct / 100), trig - 2 * urgent_gap), trig - urgent_gap

    trigger, auto_on, warn_at, urgent_at = thresholds(False)
    if used < warn_at:
        if state:  # dropped back under the line (compaction, /clear): start a new cycle
            try:
                os.remove(state_path)
            except OSError:
                pass
        return None
    trigger, auto_on, warn_at, urgent_at = thresholds(True)  # the slow path settles auto-compact on/off
    if used < warn_at:
        return None

    handoff = os.path.join(HANDOFF_DIR, sid + ".md")
    fired = set(state.get("fired", []))
    now = time.time()
    queued = compact_queued(sid, now)

    if event in ("PostToolBatch", "UserPromptSubmit"):
        kind = "urgent" if used >= urgent_at else "warn"
        if kind in fired:
            return None
        state["fired"] = sorted(fired | {"warn", kind})
        state.setdefault("warn_ts", now)
        save_state(state_path, state)
        if queued:
            return None  # compact-now already has a /compact waiting for this turn to end
        return build_message(kind, used, limit, trigger, auto_on, handoff, "fresh")

    if event == "Stop":
        if state.get("stop_nudged") or queued:
            return None
        hs = handoff_state(handoff, state.get("warn_ts", now), now)
        if hs == "fresh" and not pane_available() and auto_on:
            return None  # nothing left to ask for
        state["stop_nudged"] = True
        state["fired"] = sorted(fired | {"warn"})
        state.setdefault("warn_ts", now)
        save_state(state_path, state)
        return build_message("stop", used, limit, trigger, auto_on, handoff, hs)
    return None


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break a session over a warning
    sys.exit(0)

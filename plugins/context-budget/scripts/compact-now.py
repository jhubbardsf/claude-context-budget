#!/usr/bin/env python3
"""compact-now: let the model compact its own Claude Code session at a clean checkpoint.

Claude Code (2.1.283) has no model-callable compaction. The Skill tool refuses /compact
("a built-in CLI command, not a skill"), no hook output can request one, and every
automated way of putting text into a session (SendMessage, the inbox socket, cron, MCP
channels) delivers slash commands as plain text. The only path that runs /compact is the
human input path, so this types it into the session's own terminal pane, the same keys a
person would press. Native background workers use a temporary `claude attach <jobId>`
client in a private, detached tmux terminal, then detach without stopping the worker.

It never types while the turn is running. Measured 2026-09-26: "/compact" typed while this
script's own Bash call was still running reached Claude Code as a plain prompt and was
absorbed into the turn. So the model's call only queues the request and starts a detached
waiter. The waiter types once Claude Code's own session registry
(~/.claude/sessions/<pid>.json) reads "idle", i.e. the turn is over, stop hooks included. Typed
at an idle prompt, /compact runs like a human typed it (verified end to end in tmux).

Model usage, as the LAST tool call of a turn, after writing the handoff:
    python3 <this file> [--continue] [--dry-run]

  --continue  after the compaction, type a short "continue from the handoff" prompt so
              autonomous work resumes. Leave it off when the task is done and the session
              should wait for the user.
  --dry-run   check everything and report, queue nothing.
  --manual    for a person running it by hand: skip the handoff and main-thread checks.

Exit codes: 0 queued, 2 no fresh handoff, 3 no supported transport or matching registry
(native bg requires claude and tmux on PATH), 4 the pane couldn't be reached, 5 not called from
the main thread (a subagent's Bash would otherwise compact its PARENT session), 6 the Bash call is
sandboxed and can't see or outlive the Claude process (Linux sandbox; rerun it unsandboxed).

The user cancels a queued request by pressing Esc or typing anything: the waiter checks
the transcript for their input and stands down. It also never types:
  - into a pane whose tty isn't this Claude process's tty (or its private attach client's
    tty), or a session whose registry sessionId/procStart/jobId don't match the requester;
  - unless the input box is recognized AND empty (the top border can carry a label such as
    "── ultracode ─", so it anchors on the unlabelled bottom border). Unrecognized counts as
    "don't type";
  - while tmux is in copy mode, or during "Compacting..." (a keystroke there cancels it).
Whenever the waiter gives up it removes the request marker and leaves a
<sid>.compact-failed note, which context-budget-guard.py passes on to the model.

Typing rules, from the 2.1.283 input tokenizer: Enter must arrive as its OWN write, because a
control char is only split out of a read chunk shorter than 64 chars. So: text, pause, bare CR.

Internal entry point: --after-compact <sid> <transcript>, run by post-compact-resume.sh.
State and a log live in ~/.claude/postcompact/.state/. Python 3.9 compatible.
"""
import datetime
import fcntl
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
HANDOFF_DIR = os.path.join(HOME, ".claude", "postcompact")
STATE_DIR = os.path.join(HANDOFF_DIR, ".state")
LOG = os.path.join(STATE_DIR, "compact-now.log")
HANDOFF_MAX_AGE = 30 * 60     # context-budget-guard.py uses the same window
TURN_END_TIMEOUT = 30 * 60    # how long the waiter lets the current turn run
DRAFT_TIMEOUT = 120           # how long it waits for an empty, recognizable prompt box
COMPACT_TIMEOUT = 15 * 60     # how long a typed /compact gets to produce its boundary
READ_FAILURES = 10            # consecutive pane-read failures before giving up
CONTINUE_PROMPT = ("Compaction finished (you requested it with compact-now --continue). "
                   "Pick the task back up from the handoff above, starting at its next action.")
RULE_CHARS = set("─━")

# One script for every pane operation, so the tty check and the keystrokes share a call.
ITERM_SCRIPT = r'''
on run argv
  set targetId to item 1 of argv
  set mode to item 2 of argv
  set expectTty to item 3 of argv
  set txt to item 4 of argv
  tell application id "com.googlecode.iterm2"
    repeat with w in windows
      try
        repeat with t in tabs of w
          try
            repeat with s in sessions of t
              try
                if (unique id of s) is targetId then
                  set paneTty to (tty of s)
                  if expectTty is not "" and paneTty is not expectTty then return "ttymismatch " & paneTty
                  if mode is "type" then
                    tell s to write text txt newline no
                    delay 0.6
                    tell s to write text ""
                    return "ok " & paneTty
                  else if mode is "read" then
                    return "ok " & paneTty & linefeed & (text of s)
                  end if
                  return "ok " & paneTty
                end if
              end try
            end repeat
          end try
        end repeat
      end try
    end repeat
  end tell
  return "notfound"
end run
'''


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write("{} {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def say(msg):
    sys.stderr.write("compact-now: " + msg + "\n")


def run(cmd, **kw):
    """subprocess.run that returns None instead of raising."""
    try:
        # pane text is UTF-8 from both tmux and osascript whatever the locale says
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", **kw)
    except (OSError, subprocess.SubprocessError):
        return None


def proc_tty(pid):
    """Linux: the controlling tty from /proc/<pid>/stat, without needing procps."""
    try:
        with open("/proc/{}/stat".format(pid)) as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    tty_nr = int(fields[4]) if len(fields) > 4 and fields[4].lstrip("-").isdigit() else 0
    major, minor = (tty_nr >> 8) & 0xfff, (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00)
    if 136 <= major <= 143:
        return "/dev/pts/{}".format((major - 136) * 256 + minor)
    if major == 4 and minor < 64:
        return "/dev/tty{}".format(minor)
    return None


def claude_tty(pid):
    """/dev/ttysNNN (macOS) or /dev/pts/N (Linux) of the Claude process, if it has one."""
    if not pid:
        return None
    if os.path.isdir("/proc/{}".format(pid)):
        tty = proc_tty(pid)
        if tty:
            return tty
    res = run(["ps", "-o", "tty=", "-p", str(pid)], timeout=5)
    out = res.stdout.strip() if res else ""
    return "/dev/" + out if out and out not in ("?", "??", "-") else None


def pane_target(pid):
    """Where to type: [kind, ident, tty] with kind "iterm" or "tmux", or None."""
    tty = claude_tty(pid)
    if not tty:
        return None
    if os.environ.get("TMUX_PANE"):
        return ["tmux", os.environ["TMUX_PANE"], tty]
    iterm = os.environ.get("ITERM_SESSION_ID", "")
    if ":" in iterm:
        return ["iterm", iterm.split(":", 1)[1], tty]
    return None


def background_target(pid, sid, reg):
    """Only the native background worker's own registry can select an attach target.

    Background workers can inherit stale terminal environment variables. Their jobId,
    full sessionId and worker PID take precedence over any such pane hints.
    """
    if not reg or reg.get("kind") != "bg" or reg.get("sessionId") != sid or \
            str(reg.get("pid")) != str(pid):
        return None
    job = reg.get("jobId")
    if not isinstance(job, str) or not job or not job.isalnum():
        return None
    if not shutil.which("tmux") or not shutil.which("claude"):
        return None
    return ["bg", {"job_id": job}, None]


def attach_env():
    """Keep auth/config routing, but don't identify the attach client as a nested REPL."""
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_JOB_DIR",
                "TMUX", "TMUX_PANE", "ITERM_SESSION_ID"):
        env.pop(key, None)
    return env


def bg_tmux(target, *args):
    meta = target[1]
    return run(["tmux", "-L", meta["socket"], "-f", os.devnull] + list(args),
               timeout=10, env=attach_env())


def open_background_pane(info):
    """Attach to the existing worker through a private, detached tmux terminal.

    This never resumes a second REPL or changes worktrees. tmux supplies the PTY and
    terminal rendering so prompt inspection uses the same parser as ordinary panes.
    """
    target = info["target"]
    meta = target[1]
    if session_state(info) is None:
        return False, "background worker identity changed"
    res = run(["claude", "agents", "--json"], timeout=15, env=attach_env())
    try:
        rows = json.loads(res.stdout) if res and res.returncode == 0 else []
    except ValueError:
        rows = []
    if not isinstance(rows, list) or not any(
            isinstance(row, dict) and row.get("kind") == "background" and
            row.get("id") == meta["job_id"] and row.get("sessionId") == info["sid"] and
            str(row.get("pid")) == str(info["pid"]) for row in rows):
        return False, "claude agents did not confirm this background worker"
    meta["socket"] = "compact-now-{}-{}".format(os.getpid(), secrets.token_hex(6))
    res = bg_tmux(target, "new-session", "-d", "-s", "attach", "-x", "160", "-y", "50",
                  "-P", "-F", "#{pane_id}|#{pane_tty}|#{pane_pid}",
                  shutil.which("claude"), "attach", meta["job_id"])
    fields = res.stdout.strip().split("|") if res and res.returncode == 0 else []
    if len(fields) != 3 or not fields[0].startswith("%") or not fields[1].startswith("/dev/") \
            or not fields[2].isdigit():
        return False, "could not start the temporary claude attach terminal"
    meta.update(pane=fields[0], pane_pid=fields[2])
    target[2] = fields[1]
    log("attached background {} through private tmux {}".format(meta["job_id"], meta["socket"]))
    return True, "temporary attach to background " + meta["job_id"]


def background_pane(target, mode, text=""):
    meta = target[1]
    if not meta.get("socket"):
        if mode == "probe":
            return True, "background session {} (temporary tmux attach)".format(meta["job_id"])
        return False, "background terminal not attached"
    if not meta.get("pane"):
        return False, "background terminal not initialized"
    res = bg_tmux(target, "display-message", "-p", "-t", meta["pane"],
                  "#{pane_tty}|#{pane_pid}|#{pane_in_mode}|#{pane_dead}")
    fields = res.stdout.strip().split("|") if res and res.returncode == 0 else []
    if len(fields) != 4 or fields[:2] != [target[2], meta["pane_pid"]] or fields[3] != "0":
        return False, "temporary attach terminal exited or changed identity"
    if mode == "probe":
        return True, "background attach terminal " + meta["job_id"]
    if fields[2] != "0":
        return (True, "") if mode == "read" else (False, "temporary attach terminal is in copy mode")
    if mode == "read":
        res = bg_tmux(target, "capture-pane", "-p", "-t", meta["pane"])
        return (True, res.stdout) if res and res.returncode == 0 else (False, "attach screen unavailable")
    res = bg_tmux(target, "send-keys", "-t", meta["pane"], "-l", text)
    if not res or res.returncode != 0:
        return False, "attach text write failed"
    time.sleep(0.6)
    res = bg_tmux(target, "send-keys", "-t", meta["pane"], "Enter")
    return bool(res and res.returncode == 0), "typed through background attach"


def close_background_pane(target, detach=False):
    """Release only our attach client. Never stop the background worker or its daemon.

    Successful completion uses Claude's Ctrl+Z detach. On a failed/cancelled compact,
    disconnect the client without sending keys that could interrupt compaction.
    """
    meta = target[1]
    if not meta.get("socket"):
        return
    try:
        ok, _ = background_pane(target, "probe")
        if detach and ok:
            bg_tmux(target, "send-keys", "-t", meta["pane"], "C-z")
            time.sleep(1)
        # This socket belongs exclusively to this request; its sole child is the
        # attach client, not the daemon-owned Claude worker.
        bg_tmux(target, "kill-server")
        # tmux leaves the socket file behind when its last client detaches; it's ours alone.
        try:
            os.remove(os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp",
                                   "tmux-{}".format(os.getuid()), meta["socket"]))
        except OSError:
            pass
        log("detached private background terminal " + meta["job_id"])
    except Exception as e:
        log("background terminal cleanup failed: {!r}".format(e))


def pane(target, mode, text=""):
    """mode "probe", "read" or "type". Returns (ok, detail); "read" puts the screen in detail.
    Refuses when the pane's tty isn't the one recorded for this Claude process. A tmux pane in
    copy mode reads as an empty screen, which the caller treats as "can't verify"."""
    kind, ident, expect_tty = target
    if kind == "bg":
        return background_pane(target, mode, text)
    if kind == "iterm":
        res = run(["osascript", "-", ident, mode, expect_tty or "", text], input=ITERM_SCRIPT, timeout=20)
        if res is None:
            return False, "osascript failed or timed out"
        out = res.stdout.rstrip("\n")
        if out.startswith("ttymismatch"):
            return False, "pane tty {} isn't this session's {}; refusing".format(out.split(" ", 1)[-1], expect_tty)
        if not out.startswith("ok "):
            return False, "iTerm session {} not reachable ({})".format(ident, out or res.stderr.strip())
        first, _, screen = out.partition("\n")
        if mode == "read":
            return True, screen
        return True, ("typed into" if mode == "type" else "iTerm pane on") + " " + first[3:]
    info = run(["tmux", "display-message", "-p", "-t", ident, "#{pane_tty} #{pane_in_mode}"], timeout=10)
    fields = info.stdout.split() if info and info.returncode == 0 else []
    if len(fields) != 2:
        return False, "tmux pane {} not reachable".format(ident)
    pane_tty, in_mode = fields
    if expect_tty and pane_tty != expect_tty:
        return False, "pane tty {} isn't this session's {}; refusing".format(pane_tty, expect_tty)
    if mode == "probe":
        return True, "tmux pane {} on {}".format(ident, pane_tty)
    if mode == "read":
        if in_mode == "1":
            return True, ""  # copy mode: keys wouldn't reach Claude, so don't vouch for the box
        res = run(["tmux", "capture-pane", "-p", "-t", ident], timeout=10)
        return (True, res.stdout) if res and res.returncode == 0 else (False, "tmux capture-pane failed")
    if in_mode == "1":
        return False, "tmux pane {} is in copy mode".format(ident)
    res = run(["tmux", "send-keys", "-t", ident, "-l", text], timeout=10)
    if not res or res.returncode != 0:
        return False, "tmux send-keys failed"
    time.sleep(0.6)
    res = run(["tmux", "send-keys", "-t", ident, "Enter"], timeout=10)
    return bool(res and res.returncode == 0), "typed into tmux pane {}".format(ident)


def is_rule(line, labelled=False):
    s = line.strip()
    if len(s) < 20:
        return False
    if not labelled:
        return set(s) <= RULE_CHARS
    # A labelled top border is a run of rule chars, the label, then a rule char. The label is
    # a /rename title of any length, so no dash-ratio test: a 50-char title on a 160-col rule
    # is ~63% dashes. The caller still requires the bottom border's width and a ❯ line below.
    dashes = sum(1 for c in s if c in RULE_CHARS)
    return s[:10] == s[0] * 10 and s[0] in RULE_CHARS and s[-1] in RULE_CHARS and dashes >= 20


def prompt_box_empty(screen):
    """True when the input box is recognized and empty, False when it holds text, None when it
    can't be found. Anchors on the LAST unlabelled rule (the box's bottom border), then walks up
    to a same-width top border, which may carry a label ("── ultracode ─", a session name), and
    requires the line under it to start with the ❯ prompt."""
    lines = screen.split("\n")
    bottom = next((i for i in range(len(lines) - 1, -1, -1) if is_rule(lines[i])), None)
    if bottom is None:
        return None
    width = len(lines[bottom].rstrip())
    for top in range(bottom - 1, max(bottom - 60, -1), -1):
        if is_rule(lines[top], labelled=True) and abs(len(lines[top].rstrip()) - width) <= 2:
            if top + 1 >= bottom or not lines[top + 1].lstrip().startswith("❯"):
                return None
            box = " ".join(l.strip() for l in lines[top + 1:bottom]).replace("❯", "", 1).strip()
            return box == ""
    return None


def config_dir():
    """Claude Code keeps sessions and transcripts here; CLAUDE_CONFIG_DIR moves them. Our own
    handoff/state dir stays at ~/.claude/postcompact either way."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def registry(pid):
    try:
        with open(os.path.join(config_dir(), "sessions", "{}.json".format(pid))) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def background_worker_matches(info):
    """Cross-check the live supervisor roster, not just a possibly stale PID file.

    Attach can revive a dead job using a different REPL. The request must not follow
    that replacement. Roster authentication fields are never returned or logged.
    """
    try:
        os.kill(int(info["pid"]), 0)
        with open(os.path.join(config_dir(), "daemon", "roster.json")) as f:
            roster = json.load(f)
        worker = roster["workers"][info["target"][1]["job_id"]]
        return worker.get("sessionId") == info["sid"] and \
            str(worker.get("replPid")) == str(info["pid"]) and \
            bool(info.get("proc_start")) and worker.get("replProcStart") == info["proc_start"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def session_state(info):
    """(idle, statusUpdatedAt_ms) for the session that queued the request, or None when the pid's
    registry is gone or now belongs to a different session."""
    reg = registry(info.get("pid"))
    if not reg or reg.get("sessionId") != info.get("sid") or \
            (info.get("proc_start") and reg.get("procStart") != info.get("proc_start")):
        return None
    target = info.get("target") or []
    if target and target[0] == "bg" and \
            (reg.get("kind") != "bg" or reg.get("jobId") != target[1]["job_id"] or
             not background_worker_matches(info)):
        return None
    return reg.get("status") == "idle", reg.get("statusUpdatedAt") or 0


def iso_epoch(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def tail_records(transcript, nbytes=4 << 20):
    """Transcript records from the tail, newest first."""
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - nbytes))
            data = f.read()
    except (OSError, TypeError):
        return
    for line in reversed(data.split(b"\n")):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def user_acted_since(transcript, after, ignore_compact=None):
    """True for a human prompt, queued prompt or interruption since `after`.

    During the background boundary wait, exclude exactly one own /compact command.
    Other input, including a second /compact or an interruption, cancels continuation.
    """
    for rec in tail_records(transcript):
        ts = iso_epoch(rec.get("timestamp"))
        if ts is None:
            continue
        if ts < after - 1:
            return False
        if rec.get("isSidechain") or rec.get("isMeta") or rec.get("isCompactSummary"):
            continue
        human = (rec.get("origin") or {}).get("kind") == "human"
        if rec.get("type") == "user":
            content = (rec.get("message") or {}).get("content")
            texts = [content] if isinstance(content, str) else \
                [b.get("text") or "" for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"]
            if any(t.startswith("[Request interrupted by user") for t in texts):
                return True
            compact_command = len(texts) == 1 and (
                (human and texts[0].strip() == "/compact") or
                re.fullmatch(r"<command-name>/compact</command-name>"
                             r"(?:\s*<command-message>compact</command-message>)?"
                             r"(?:\s*<command-args>\s*</command-args>)?", texts[0].strip()))
            if ignore_compact is not None and compact_command:
                if ignore_compact:
                    ignore_compact = False
                    continue
                return True
            if ignore_compact is not None and any("<command-name>" in t for t in texts):
                # Local slash commands can omit origin.kind entirely. Only the
                # exact empty-argument command above belongs to this helper.
                return True
            if human:
                return True
        elif rec.get("type") == "attachment":
            att = rec.get("attachment") or {}
            if att.get("type") == "queued_command" and (att.get("origin") or {}).get("kind") == "human":
                return True
    return False


def last_boundary(transcript):
    """(epoch, trigger) of the newest compact_boundary in the transcript tail, or (0, None)."""
    for rec in tail_records(transcript):
        if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
            return iso_epoch(rec.get("timestamp")) or 0, (rec.get("compactMetadata") or {}).get("trigger")
    return 0, None


def type_at_idle_prompt(info, text, after, deadline, wanted):
    """Type `text` once the session has gone idle since `after` with a recognizable, empty prompt
    box and the user hasn't acted since `after`. Returns "typed" or the reason it didn't."""
    target = info["target"]
    transcript = info.get("transcript")
    fails, unsure_since = 0, None
    while time.time() < deadline:
        if not wanted():
            return "cancelled"
        state = session_state(info)
        if state is None:
            return "session gone"
        idle, updated = state
        if not idle or updated / 1000.0 < after:
            unsure_since = None
            time.sleep(1)
            continue
        time.sleep(1.0)  # let a just-ended turn settle
        if session_state(info) != (True, updated):
            continue
        if transcript and user_acted_since(transcript, after):
            return "the user acted"
        ok, screen = pane(target, "read")
        if not ok:
            fails += 1
            log("pane read failed ({}/{}): {}".format(fails, READ_FAILURES, screen))
            if fails >= READ_FAILURES:
                return "pane unreachable"
            time.sleep(2)
            continue
        fails = 0
        empty = prompt_box_empty(screen)
        if empty is not True:
            unsure_since = unsure_since or time.time()
            if time.time() - unsure_since > DRAFT_TIMEOUT:
                return "prompt box held a draft" if empty is False else "prompt box not recognized"
            time.sleep(2)
            continue
        # last checks, as close to the keystrokes as possible
        if not wanted():
            return "cancelled"
        if session_state(info) != (True, updated):
            continue
        if target[0] != "bg" and claude_tty(info.get("pid")) != target[2]:
            continue
        if self_command_active(info.get("sid")):
            continue  # a self-command waiter is mid-delivery; it yields to us within a second
        info["input_started_at"] = time.time()
        ok, detail = pane(target, "type", text)
        log("typed {!r}: {} ({})".format(text[:40], ok, detail))
        return "typed" if ok else "type failed: " + detail
    return "never went idle"


def self_command_active(sid):
    """True while self-command.py holds this session's lock (it flocks <sid>.self-command.lock for
    its waiter's lifetime). It stands down as soon as it sees our marker, so this only waits out
    a delivery already in flight; the two never type into one box at once."""
    try:
        fd = os.open(os.path.join(STATE_DIR, "{}.self-command.lock".format(sid)), os.O_RDONLY)
    except (OSError, TypeError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def marker_path(sid):
    return os.path.join(STATE_DIR, sid + ".compact-requested")


def read_marker(sid):
    try:
        with open(marker_path(sid)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = "{}.{}".format(path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def drop_marker(sid, ts, reason):
    """Remove the marker if it's still ours, and leave a note the guard passes to the model."""
    info = read_marker(sid)
    if info and info.get("ts") == ts:
        try:
            os.remove(marker_path(sid))
        except OSError:
            pass
        if reason:
            write_json(os.path.join(STATE_DIR, sid + ".compact-failed"), {"ts": time.time(), "reason": reason})
    log("request {} dropped: {}".format(sid, reason or "done"))


def find_transcript(sid):
    hits = glob.glob(os.path.join(config_dir(), "projects", "*", sid + ".jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def called_from_main_thread(transcript):
    """True when the main transcript holds an unanswered Bash call to compact-now, i.e. this runs
    as the main thread's tool call. A subagent's calls are written under subagents/ instead."""
    answered = set()
    for rec in tail_records(transcript, 1 << 20):
        if rec.get("isSidechain"):
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            if rec.get("type") == "user" and (rec.get("origin") or {}).get("kind") == "human":
                return False  # reached the prompt that started this turn
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                answered.add(block.get("tool_use_id"))
            elif block.get("type") == "tool_use" and block.get("name") == "Bash" \
                    and block.get("id") not in answered:
                if "compact-now" in str((block.get("input") or {}).get("command", "")):
                    return True
    return False


def request(argv):
    cont = "--continue" in argv
    dry = "--dry-run" in argv
    manual = "--manual" in argv
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    pid = os.environ.get("CLAUDE_PID", "")
    if not sid or not pid:
        say("run this from a Claude Code session (CLAUDE_CODE_SESSION_ID/CLAUDE_PID unset).")
        return 3
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        say("this Bash call can't see the Claude process, which means it's sandboxed, and a sandboxed "
            "call can't outlive the turn either. Rerun the same command with the sandbox disabled for it "
            "(dangerouslyDisableSandbox, or add it to sandbox.excludedCommands).")
        return 6
    except (PermissionError, ValueError):
        pass
    reg = registry(pid)
    is_bg = bool(reg and reg.get("kind") == "bg")
    target = background_target(pid, sid, reg) if is_bg else pane_target(pid)
    if not is_bg and (os.environ.get("TMUX_PANE") or os.environ.get("ITERM_SESSION_ID")) and not claude_tty(pid):
        say("couldn't read the Claude process's tty (on Linux, is /proc mounted or procps installed?). "
            "Nothing queued; auto-compact will still pick up the handoff when it fires.")
        return 3
    if not reg or reg.get("sessionId") != sid or not target:
        say("no supported terminal or matching session registry. Native background sessions need "
            "claude and tmux on PATH and a matching worker jobId; other sessions need an iTerm or tmux pane. "
            "Nothing queued; enabled auto-compact will still pick up the handoff when it fires.")
        return 3
    transcript = find_transcript(sid)
    if not manual:
        deadline = time.time() + 3  # the running tool_use is flushed before the tool starts
        while not (transcript and called_from_main_thread(transcript)):
            if time.time() > deadline:
                say("refused: this isn't the main session's own tool call (a subagent would compact its "
                    "parent). Only the main thread can compact its session.")
                return 5
            time.sleep(0.3)
            transcript = transcript or find_transcript(sid)
        handoff = os.path.join(HANDOFF_DIR, sid + ".md")
        try:
            age = time.time() - os.path.getmtime(handoff)
        except OSError:
            age = None
        if age is None or age > HANDOFF_MAX_AGE:
            say("refused: write the handoff to {} first (task, decisions, done so far, exact next action, "
                "files in play, gotchas). It's reloaded right after compaction.".format(handoff))
            return 2
    ok, detail = pane(target, "probe")
    if not ok:
        say(detail)
        return 4
    if dry:
        print("dry run: would type /compact into the {} once the turn ends{}".format(
            detail, ", then resume" if cont else ""))
        return 0
    now = time.time()
    info = {"ts": now, "continue": cont, "target": target, "pid": pid, "sid": sid,
            "proc_start": reg.get("procStart"), "transcript": transcript}
    try:
        write_json(marker_path(sid), info)
    except OSError as e:
        say("couldn't write the request marker in {}: {}".format(STATE_DIR, e))
        return 4
    try:
        os.remove(os.path.join(STATE_DIR, sid + ".compact-failed"))
    except OSError:
        pass
    log("queued for {} ({}), continue={}".format(sid, detail, cont))
    if daemonize():
        waiter(info)
    print("Queued: /compact will be typed into this session's pane ({}) once this turn ends{}. End the "
          "turn now and don't start anything else. The user can cancel it by pressing Esc or typing.".format(
              detail, ", then the task resumes from the handoff" if cont else ", then the session waits for the user"))
    return 0


def daemonize():
    """Double-fork into a new session with stdio on /dev/null. True in the detached child.
    The intermediate child never returns: if setsid or the second fork fails there, it exits
    rather than carry on as a second copy of the caller."""
    if os.fork():
        return False
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
    except BaseException:
        os._exit(1)
    return True


def waiter(info):
    """Detached: type /compact at the next idle prompt, then watch for the compaction to land."""
    if info["target"][0] == "bg":
        background_waiter(info)
        os._exit(0)
    sid, ts = info["sid"], info["ts"]
    try:
        still_queued = lambda: (read_marker(sid) or {}).get("ts") == ts  # noqa: E731
        outcome = type_at_idle_prompt(info, "/compact", ts, ts + TURN_END_TIMEOUT, still_queued)
        if outcome != "typed":
            drop_marker(sid, ts, None if outcome == "cancelled" else outcome)
            os._exit(0)
        typed_at = time.time()
        current = read_marker(sid)
        if current and current.get("ts") == ts:
            current["typed_at"] = typed_at
            write_json(marker_path(sid), current)
        # after_compact consumes the marker when the compaction lands; if it never does
        # (cancelled by a keystroke, absorbed, errored), clean up so nothing fires later.
        deadline = typed_at + COMPACT_TIMEOUT
        while time.time() < deadline and still_queued():
            time.sleep(2)
        if still_queued():
            drop_marker(sid, ts, "/compact was typed but no compaction followed")
    except Exception as e:  # noqa: BLE001 - detached, the log is the only place to report
        log("waiter crashed: {!r}".format(e))
        drop_marker(sid, ts, "compact-now's waiter crashed")
    os._exit(0)


def background_waiter(info):
    """One owner handles attach, compact, optional continuation, and detach.

    SessionStart still reloads the handoff, but leaves this request's marker alone.
    Keeping terminal ownership here avoids a cleanup/resume race between two helpers.
    """
    sid, ts = info["sid"], info["ts"]
    target = info["target"]
    reason, detach = None, False
    queued = lambda: (read_marker(sid) or {}).get("ts") == ts  # noqa: E731
    try:
        deadline = ts + TURN_END_TIMEOUT
        before = last_boundary(info.get("transcript"))
        if before[0] >= ts:
            reason = "session already compacted after this request"
            return
        # Attaching must wait for the current tool call and all Stop hooks to end.
        while time.time() < deadline:
            if not queued():
                return
            state = session_state(info)
            if state is None:
                reason = "background worker identity changed before attach"
                return
            if user_acted_since(info.get("transcript"), ts):
                reason = "the user acted before attach"
                return
            if last_boundary(info.get("transcript")) != before:
                reason = "session compacted before the requested attach"
                return
            if state[0] and state[1] / 1000.0 >= ts:
                break
            time.sleep(1)
        else:
            reason = "background session never went idle"
            return
        ok, detail = open_background_pane(info)
        if not ok:
            reason = detail
            return
        # Persist terminal metadata for diagnosis. The SessionStart hook sees bg and
        # defers to this waiter; it never takes ownership of this terminal.
        if not queued():
            return
        write_json(marker_path(sid), info)
        wanted = lambda: queued() and last_boundary(info.get("transcript")) == before  # noqa: E731
        outcome = type_at_idle_prompt(info, "/compact", ts, deadline, wanted)
        if outcome != "typed":
            reason = None if outcome == "cancelled" else outcome
            return
        typed_at = time.time()
        info["typed_at"] = typed_at
        if queued():
            write_json(marker_path(sid), info)
        deadline = typed_at + COMPACT_TIMEOUT
        while time.time() < deadline:
            if not queued():
                return
            if session_state(info) is None:
                reason = "background worker identity changed during compaction"
                return
            if user_acted_since(info.get("transcript"), info.get("input_started_at", typed_at),
                                ignore_compact=True):
                reason = "the user acted after /compact; continuation cancelled"
                return
            when, trigger = last_boundary(info.get("transcript"))
            if (when, trigger) != before and when >= typed_at - 5:
                if trigger != "manual":
                    reason = "boundary was not the requested manual compaction"
                    return
                log("background manual compaction confirmed for " + sid)
                if info.get("continue"):
                    outcome = type_at_idle_prompt(info, CONTINUE_PROMPT, when,
                                                  time.time() + 300, queued)
                    log("background resume prompt: " + outcome)
                    if outcome != "typed":
                        reason = "compacted, but resume prompt was not sent: " + outcome
                        return
                detach = True
                return
            time.sleep(1)
        reason = "/compact was typed but no new manual compaction followed"
    except Exception as e:
        log("background waiter crashed: {!r}".format(e))
        reason = "compact-now's background waiter crashed"
    finally:
        close_background_pane(target, detach=detach)
        drop_marker(sid, ts, reason)


def after_compact(sid, transcript):
    """Handle interactive requests after compaction; native bg stays with its owning waiter.

    For interactive requests, consumes the marker and sends continuation only when
    the request's own typed /compact produced this boundary.
    """
    info = read_marker(sid)
    if info and (info.get("target") or [None])[0] == "bg":
        # The original bg waiter watches the boundary and owns terminal cleanup.
        return 0
    try:
        os.remove(marker_path(sid))
    except OSError:
        pass
    if not info or not info.get("continue") or not info.get("typed_at"):
        return 0
    if time.time() - info.get("ts", 0) > TURN_END_TIMEOUT + COMPACT_TIMEOUT:
        log("stale request marker for {}; no resume prompt".format(sid))
        return 0
    if not daemonize():
        return 0
    try:
        info["transcript"] = transcript or info.get("transcript")
        typed_at = info["typed_at"]
        deadline = time.time() + COMPACT_TIMEOUT
        while time.time() < deadline:
            when, trigger = last_boundary(info["transcript"])
            if when >= typed_at - 5:
                if trigger != "manual" or when > typed_at + COMPACT_TIMEOUT:
                    log("boundary {} isn't the requested compaction; no resume prompt".format(trigger))
                    break
                outcome = type_at_idle_prompt(info, CONTINUE_PROMPT, when, time.time() + 300, lambda: True)
                log("resume prompt: " + outcome)
                break
            time.sleep(1)
    except Exception as e:  # noqa: BLE001
        log("resume waiter crashed: {!r}".format(e))
    os._exit(0)


def main(argv):
    if argv[:1] == ["--after-compact"] and len(argv) >= 3:
        return after_compact(argv[1], argv[2])
    return request(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

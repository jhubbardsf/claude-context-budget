#!/usr/bin/env python3
"""self-command: type an allowlisted slash command into a Claude Code session's own prompt.

Claude Code gives the model (and outside tools) no way to run slash commands. compact-now.py
already solved this for /compact: wait until the session is idle with an empty input box, type
the command the way a person would (the session's iTerm2/tmux pane, or a private
`claude attach <jobId>` terminal for native background workers), then prove from the transcript
that it landed. This reuses that transport, loaded from compact-now.py next to this file, for a
short allowlist of commands. /compact itself stays with compact-now.py.

Usage:
  self-command.py rename "<title>"            from inside a session: after this turn ends
  self-command.py color <name>                red blue green yellow purple orange pink cyan default
  self-command.py remote-control              (alias: rc)
  self-command.py --pid <pid> <command> ...   drive another live session (typed once it's idle)
  self-command.py rc-sweep [--dry-run]        re-run /remote-control in every live session whose
                                              Remote Control dropped because the signed-in account
                                              changed (cc-account switch, /login)
  self-command.py rc-watch [--interval 15]    rc-sweep forever; what the LaunchAgent runs
  self-command.py install-watch | uninstall-watch | status
  --dry-run                                   resolve and report, type nothing
  --wait                                      with --pid: stay in the foreground and print the result

Exit codes: 0 queued/done (or already in that state), 1 --wait result not verified, 2 bad
arguments, 3 no supported transport or session, 4 busy (another command or a compact-now request
is pending), over the rate limit, or Remote Control already connected, 5 not the main thread's
own call, 6 command not allowed.

Safety: the command must be on the allowlist and its argument must validate (a conservative
character set, so nothing opens autocomplete or turns Enter into a newline); a deny list (clear,
exit, login, ...) is checked separately. It types only into an idle session whose recognized input
box is empty, never alongside a compact-now request (compact-now wins), and one command per
session at a time, enforced with a kernel flock that compact-now also honours. Every command is
verified by a transcript record written after the keystrokes; nothing unverified is reported as
success. State and a log live in ~/.claude/postcompact/.state/. Python 3.9 compatible.
"""
import fcntl
import glob
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "postcompact", ".state")
LABEL = "dev.joshuahubbard.cc-self-command-rc"
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")


def _load_transport():
    spec = importlib.util.spec_from_file_location("compact_now", os.path.join(HERE, "compact-now.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LOG = os.path.join(mod.STATE_DIR, "self-command.log")  # the shared transport logs here too
    return mod


try:
    cn = _load_transport()
    STATE_DIR = cn.STATE_DIR
except (OSError, ImportError, SyntaxError) as _e:  # uninstall-watch/status must still work
    cn = None
    _CN_ERROR = _e

IDLE_WAIT = 30 * 60          # how long one waiter waits for the session to go idle
VERIFY_WAIT = 45             # how long a typed command gets to show up in the transcript
RC_MAX_TYPED = 2             # /remote-control keystrokes per disconnect before giving up
RC_MAX_FAILS = 6             # transport failures (unreachable pane, no pane, ...) per disconnect
RC_LOOKBACK = 24 * 3600      # ignore disconnects older than this
RC_ACCOUNT_CHANGED = "Remote Control disconnected — signed-in claude.ai account or organization changed"
COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan", "default")
DENY = {"clear", "exit", "quit", "logout", "login", "permissions", "config", "resume", "init",
        "install", "plugin", "plugins", "mcp", "hooks", "doctor", "upgrade", "compact", "memory",
        "add-dir", "agents", "model", "privacy-settings", "terminal-setup", "vim", "bug", "feedback"}
ALIASES = {"rc": "remote-control"}
# Outcomes that mean "try again later", not "this session is broken". They never count toward the
# give-up cap; the watcher simply re-queues on a later sweep.
WAIT_OUTCOMES = ("never went idle", "prompt box held a draft", "prompt box not recognized",
                 "deferred to compact-now", "no longer needed", "busy", "background session never went idle",
                 "stopped by a signal")


def log(msg):
    if cn:
        cn.log(msg)


def say(msg):
    sys.stderr.write("self-command: " + msg + "\n")


# ---- transcript reading --------------------------------------------------------------------

_RC_INC = {}   # path -> {"ino", "size", "result"}: incremental scan state for rc_state


def _rc_scan(data):
    """Newest RC record in a byte chunk, or None. The '"type":"system"' byte test only matches real
    system records: text quoted inside another record has escaped quotes."""
    for line in reversed(data.split(b"\n")):
        if b'"type":"system"' not in line or \
                (b"bridge_status" not in line and b"Remote Control disconnected" not in line):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "system":
            continue
        ts = cn.iso_epoch(rec.get("timestamp")) or 0
        content = rec.get("content") or ""
        if rec.get("subtype") == "bridge_status" and "/remote-control is active" in content:
            return "active", ts, rec.get("uuid")
        if rec.get("subtype") == "informational" and content.startswith("Remote Control disconnected"):
            return ("account" if content.startswith(RC_ACCOUNT_CHANGED) else "other"), ts, rec.get("uuid")
    return None


def rc_state(transcript, since=0):
    """Newest Remote Control event: ("active"|"account"|"other", epoch, uuid), or None.

    The watcher asks every few seconds for every live session, so after the first read it only
    scans bytes appended since the last call. The first read covers the last 16MB; a disconnect
    older than that is invisible, which is fine because RC_LOOKBACK retires it anyway."""
    try:
        st = os.stat(transcript)
    except (OSError, TypeError):
        return None
    prev = _RC_INC.get(transcript)
    if prev and prev["ino"] == st.st_ino and prev["done"] <= st.st_size:
        start, result, fresh = prev["done"], prev["result"], False
    else:
        start, result, fresh = max(0, st.st_size - (16 << 20)), None, True
    done = start
    if start < st.st_size:
        try:
            with open(transcript, "rb") as f:
                f.seek(start)
                data = f.read(st.st_size - start)
        except OSError:
            data = b""
        base = start
        if fresh and start > 0:  # the tail starts mid-line; drop the fragment
            nl = data.find(b"\n")
            data, base = (data[nl + 1:], start + nl + 1) if nl >= 0 else (b"", start)
        cut = data.rfind(b"\n")  # only complete lines; an unfinished last line waits for next time
        if cut >= 0:
            result = _rc_scan(data[:cut + 1]) or result
            done = base + cut + 1
        else:
            done = base
    if len(_RC_INC) > 512:
        _RC_INC.clear()
    _RC_INC[transcript] = {"ino": st.st_ino, "done": done, "result": result}
    if result and since and result[1] and result[1] < since:
        return None
    return result


def records_after(transcript, offset):
    """Parsed records appended after byte `offset`."""
    try:
        with open(transcript, "rb") as f:
            f.seek(offset)
            data = f.read()
    except (OSError, TypeError):
        return []
    out = []
    for line in data.split(b"\n"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def last_record(transcript, rtype, field):
    for rec in cn.tail_records(transcript, 8 << 20):
        if rec.get("type") == rtype:
            return rec.get(field)
    return None


def tx(info):
    """The session's transcript path, looked up again until it exists (a brand-new session has
    none until its first record, which can be the command itself)."""
    if not info.get("transcript"):
        info["transcript"] = cn.find_transcript(info["sid"])
    return info.get("transcript")


def tx_size(info):
    try:
        return os.path.getsize(tx(info))
    except (OSError, TypeError):
        return 0


# ---- commands ------------------------------------------------------------------------------

def verify_rc(info, arg, typed_at, offset):
    """Judged only on what was written after the keystrokes: a bridge_status "active" record (or
    the registry gaining a bridgeSessionId) is success, a new disconnect record is failure."""
    for rec in reversed(records_after(tx(info), offset)):
        if rec.get("type") != "system":
            continue
        content = rec.get("content") or ""
        if rec.get("subtype") == "bridge_status" and "/remote-control is active" in content:
            return True, "Remote Control is active"
        if rec.get("subtype") == "informational" and content.startswith("Remote Control disconnected"):
            return False, "Remote Control reported a disconnect after the command"
    if (cn.registry(info["pid"]) or {}).get("bridgeSessionId"):
        return True, "Remote Control is active (registry)"
    return None, ""


def verify_rename(info, arg, typed_at, offset):
    # custom-title records carry no timestamp, so require one appended after the keystrokes.
    if any(r.get("type") == "custom-title" and r.get("customTitle") == arg for r in records_after(tx(info), offset)):
        return True, "renamed"
    return None, ""


def verify_color(info, arg, typed_at, offset):
    if any(r.get("type") == "agent-color" and r.get("agentColor") == arg for r in records_after(tx(info), offset)):
        return True, "color set"
    return None, ""


def already_rename(info, arg):
    return (cn.registry(info["pid"]) or {}).get("name") == arg


def already_color(info, arg):
    return last_record(tx(info), "agent-color", "agentColor") == arg


def already_rc(info, arg):
    return not rc_needed(info)


# A title is typed after "/rename " and submitted with Enter, so it must not end in "\" (Enter
# becomes a newline), and must not contain "@" (opens file-mention autocomplete, which eats Enter).
TITLE_RE = re.compile(r"[\w .,:;()'&+\-|!?\[\]#/~=]{1,80}", re.UNICODE)


def valid_title(arg):
    return isinstance(arg, str) and bool(TITLE_RE.fullmatch(arg)) and arg.strip() == arg \
        and not arg.startswith("/")


COMMANDS = {
    # name: (argument validator or None for no argument, verifier, already-in-that-state check,
    #        (max typed runs, window seconds))
    "remote-control": (None, verify_rc, already_rc, (4, 3600)),
    "rename": (valid_title, verify_rename, already_rename, (3, 3600)),
    "color": (lambda a: a in COLORS, verify_color, already_color, (6, 3600)),
}


def parse_command(words):
    """(name, arg, text). Raises PermissionError (exit 6) or ValueError (exit 2)."""
    if len(words) == 1 and " " in words[0].strip():
        words = words[0].strip().split(" ", 1)  # a whole command passed as one quoted string
    if not words or not words[0].strip():
        raise ValueError("no command given")
    name = words[0].lstrip("/").lower()
    name = ALIASES.get(name, name)
    if name in DENY:
        raise PermissionError("/{} is never typed by self-command".format(name))
    if name not in COMMANDS:
        raise PermissionError("/{} isn't on the allowlist ({})".format(name, ", ".join(sorted(COMMANDS))))
    validator = COMMANDS[name][0]
    rest = words[1:]
    if validator is None:
        if rest:
            raise ValueError("/{} takes no arguments".format(name))
        return name, None, "/" + name
    arg = " ".join(rest)
    if "\n" in arg or "\r" in arg or not validator(arg):
        raise ValueError("invalid argument for /{}: {!r} (titles: 1-80 letters, digits, spaces and "
                         "simple punctuation; no @ or backslash)".format(name, arg))
    return name, arg, "/{} {}".format(name, arg)


# ---- targets -------------------------------------------------------------------------------

ITERM_LIST = r'''
tell application id "com.googlecode.iterm2"
  set out to ""
  repeat with w in windows
    try
      repeat with t in tabs of w
        try
          repeat with s in sessions of t
            try
              set out to out & (unique id of s) & "|" & (tty of s) & linefeed
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return out
end tell
'''


def pane_for_tty(tty):
    """Find the iTerm2 session or default-socket tmux pane showing this tty, from outside it."""
    if not tty:
        return None
    if shutil.which("tmux"):
        res = cn.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_tty}"], timeout=10)
        for line in (res.stdout.splitlines() if res and res.returncode == 0 else []):
            pid_, _, ptty = line.partition(" ")
            if ptty.strip() == tty:
                return ["tmux", pid_, tty]
    if sys.platform == "darwin":
        running = cn.run(["osascript", "-e", 'application id "com.googlecode.iterm2" is running'], timeout=10)
        if running and running.stdout.strip() == "true":
            res = cn.run(["osascript", "-"], input=ITERM_LIST, timeout=20)
            for line in (res.stdout.splitlines() if res and res.returncode == 0 else []):
                uid, _, ptty = line.partition("|")
                if ptty.strip() == tty and uid:
                    return ["iterm", uid.strip(), tty]
    return None


def live_proc_start(pid):
    # Claude Code writes procStart as ps's lstart in UTC; match it (and the C locale's names).
    res = cn.run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=5,
                 env=dict(os.environ, TZ="UTC", LC_ALL="C"))
    return " ".join(res.stdout.split()) if res and res.returncode == 0 and res.stdout.strip() else None


def proc_matches(reg):
    """The pid in a registry file still belongs to that Claude process (pid reuse after a crash
    would otherwise point us at whatever took the pid, e.g. the shell that got the pane back)."""
    want = " ".join(str(reg.get("procStart") or "").split())
    got = live_proc_start(reg.get("pid"))
    return bool(want and got and want == got)


def resolve(pid, sid=None, from_inside=False):
    """Session info for the transcript/registry checks plus a transport target, or (None, reason)."""
    reg = cn.registry(pid)
    if not reg:
        return None, "no session registry for pid {}".format(pid)
    if sid and reg.get("sessionId") != sid:
        return None, "registry for pid {} belongs to another session".format(pid)
    sid = reg.get("sessionId")
    if not proc_matches(reg):
        return None, "pid {} isn't the Claude process its registry describes (exited or reused)".format(pid)
    if reg.get("spare"):
        return None, "pid {} is an unclaimed spare".format(pid)
    if reg.get("kind") == "bg":
        target = cn.background_target(str(pid), sid, reg)
        if not target:
            return None, "background session needs claude and tmux on PATH and a jobId"
    elif reg.get("kind") == "interactive":
        target = cn.pane_target(pid) if from_inside else None
        target = target or pane_for_tty(cn.claude_tty(pid))
        if not target:
            return None, "no iTerm2 or tmux pane shows this session's tty"
    else:
        return None, "unsupported session kind {!r}".format(reg.get("kind"))
    return {"pid": str(pid), "sid": sid, "proc_start": reg.get("procStart"), "target": target,
            "transcript": cn.find_transcript(sid), "name": reg.get("name")}, ""


# ---- per-session state ---------------------------------------------------------------------

def lock_path(sid):
    return os.path.join(STATE_DIR, sid + ".self-command.lock")


def info_path(sid):
    return os.path.join(STATE_DIR, sid + ".self-command.json")


def history_path(sid):
    return os.path.join(STATE_DIR, sid + ".self-command-history.json")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def acquire(sid, command):
    """Take the session's flock (non-blocking). Returns the fd, or None when someone holds it.
    The kernel drops it when the holder dies, so there's no stale lock and no pid-reuse problem."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(lock_path(sid), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    cn.write_json(info_path(sid), {"command": command, "ts": time.time(), "waiter": os.getpid()})
    return fd


def lock_held(sid):
    try:
        fd = os.open(lock_path(sid), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def compact_pending(sid):
    return os.path.exists(cn.marker_path(sid))


def busy_reason(sid):
    """Why a new command can't be queued for this session right now, or None."""
    if compact_pending(sid):
        return "a compact-now request is pending"
    if lock_held(sid):
        return "/{} is already queued".format((read_json(info_path(sid), {}) or {}).get("command", "?"))
    return None


def over_limit(sid, name):
    runs, window = COMMANDS[name][3]
    hist = read_json(history_path(sid), [])
    recent = [h for h in hist if isinstance(h, dict) and h.get("command") == name and
              time.time() - h.get("ts", 0) < window and h.get("typed")]
    return len(recent) >= runs


def record(sid, name, result, typed):
    hist = [h for h in read_json(history_path(sid), []) if isinstance(h, dict) and time.time() - h.get("ts", 0) < 86400]
    hist.append({"command": name, "ts": time.time(), "result": result, "typed": typed})
    cn.write_json(history_path(sid), hist[-50:])


# ---- the prompt box ------------------------------------------------------------------------

PLACEHOLDER = re.compile(r'Try "[^"\n]{1,160}"')
SGR = re.compile(r"\x1b\[([0-9;:]*)m")


def box_row(lines):
    """Index of the input box's (single) text row, from the same anchoring compact-now uses."""
    bottom = next((i for i in range(len(lines) - 1, -1, -1) if cn.is_rule(lines[i])), None)
    if bottom is None or bottom < 2 or not cn.is_rule(lines[bottom - 2], labelled=True):
        return None
    return bottom - 1


def ghost_only(styled_line):
    """True when every visible character after the ❯ is dim/grey (a placeholder or a prompt
    suggestion), judged from tmux `capture-pane -e` SGR codes."""
    dim, seen, text = False, False, False
    pos = 0
    for mt in SGR.finditer(styled_line + "\x1b[m"):
        chunk = styled_line[pos:mt.start()]
        for ch in chunk:
            if not seen:
                seen = ch == "❯"
                continue
            if ch.strip():
                text = True
                if not dim:
                    return False
        codes = [c for c in re.split("[;:]", mt.group(1) or "0") if c != ""] or ["0"]
        for i, c in enumerate(codes):
            if c in ("0", "22"):
                dim = False
            elif c == "2" or c in ("90",) or (c == "38" and codes[i + 1:i + 3][:1] == ["5"] and
                                              codes[i + 2:i + 3] and 232 <= int(codes[i + 2] or 0) <= 250):
                dim = True
            elif c == "38" and codes[i + 1:i + 2] == ["2"] and len(codes) >= i + 5:
                r, g, b = (int(x or 0) for x in codes[i + 2:i + 5])
                dim = abs(r - g) < 16 and abs(g - b) < 16 and r < 170  # grey truecolor
        pos = mt.end()
    return seen and text


def box_empty(screen, target=None):
    """cn.prompt_box_empty, except dim ghost text counts as empty: a fresh session's
    `❯ Try "..."` placeholder, or a prompt suggestion. On tmux/bg panes the styling is read
    with capture-pane -e; on iTerm (plain text only) just the Try "..." placeholder is accepted."""
    empty = cn.prompt_box_empty(screen)
    if empty is not False:
        return empty
    lines = screen.split("\n")
    row = box_row(lines)
    if row is None:
        return False
    if target and target[0] in ("tmux", "bg"):
        styled = styled_screen(target)
        if styled is not None:
            slines = styled.split("\n")
            srow = box_row([SGR.sub("", l) for l in slines])
            return srow is not None and ghost_only(slines[srow])
    box = lines[row].strip()
    return box.startswith("❯") and bool(PLACEHOLDER.fullmatch(box[1:].strip()))


def styled_screen(target):
    kind, ident, _ = target
    if kind == "bg":
        res = cn.bg_tmux(target, "capture-pane", "-e", "-p", "-t", ident["pane"])
    else:
        res = cn.run(["tmux", "capture-pane", "-e", "-p", "-t", ident], timeout=10)
    return res.stdout if res and res.returncode == 0 else None


# ---- the waiter ----------------------------------------------------------------------------

def type_when_idle(info, text, after, deadline, wanted):
    """cn.type_at_idle_prompt without the compact-specific rules: an already-idle session is fine
    when `after` is 0, later human input doesn't cancel (wanted() decides that), and a pending
    compact-now request always wins."""
    target = info["target"]
    fails, unsure_since = 0, None
    while time.time() < deadline:
        if compact_pending(info["sid"]):
            return "deferred to compact-now"
        if not wanted():
            return "no longer needed"
        state = cn.session_state(info)
        if state is None:
            return "session gone"
        idle, updated = state
        if not idle or (after and updated / 1000.0 < after):
            unsure_since = None
            time.sleep(1)
            continue
        time.sleep(1.0)
        if cn.session_state(info) != (True, updated):
            continue
        ok, screen = cn.pane(target, "read")
        if not ok:
            fails += 1
            log("pane read failed ({}/{}): {}".format(fails, cn.READ_FAILURES, screen))
            if fails >= cn.READ_FAILURES:
                return "pane unreachable"
            time.sleep(2)
            continue
        fails = 0
        empty = box_empty(screen, target)
        if empty is not True:
            unsure_since = unsure_since or time.time()
            if time.time() - unsure_since > cn.DRAFT_TIMEOUT:
                return "prompt box held a draft" if empty is False else "prompt box not recognized"
            time.sleep(2)
            continue
        if not wanted() or compact_pending(info["sid"]):
            continue
        if cn.session_state(info) != (True, updated):
            continue
        if target[0] != "bg" and cn.claude_tty(info.get("pid")) != target[2]:
            continue
        info["typed_status_at"] = updated
        ok, detail = cn.pane(target, "type", text)
        log("typed {!r} into {}: {} ({})".format(text[:60], info["sid"][:8], ok, detail))
        return "typed" if ok else "type failed: " + detail
    return "never went idle"


ITERM_ESC = r'''
on run argv
  tell application id "com.googlecode.iterm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (unique id of s) is (item 1 of argv) and (tty of s) is (item 2 of argv) then
            tell s to write text (ASCII character 27) newline no
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''


def send_escape(target):
    kind, ident, tty = target
    if kind == "bg":
        res = cn.bg_tmux(target, "send-keys", "-t", ident["pane"], "Escape")
    elif kind == "tmux":
        res = cn.run(["tmux", "send-keys", "-t", ident, "Escape"], timeout=10)
    else:
        res = cn.run(["osascript", "-", ident, tty or ""], input=ITERM_ESC, timeout=20)
        return bool(res and res.stdout.strip() == "ok")
    return bool(res and res.returncode == 0)


def back_out_of_rc_picker(info, typed_at):
    """/remote-control on a session that reconnected under us opens its Disconnect / Show QR /
    Continue picker and leaves the session "waiting". Press Esc (= Continue) only when that exact
    picker is on screen, the session went "waiting" right after our keystrokes, and nobody has
    typed since."""
    reg = cn.registry(info["pid"]) or {}
    if reg.get("sessionId") != info["sid"] or reg.get("status") != "waiting" or reg.get("waitingFor"):
        return False
    if (reg.get("statusUpdatedAt") or 0) / 1000.0 < typed_at - 1 or \
            (reg.get("statusUpdatedAt") or 0) / 1000.0 > typed_at + 15:
        return False
    if cn.user_acted_since(tx(info), typed_at + 0.5):
        return False
    ok, screen = cn.pane(info["target"], "read")
    tail = "\n".join([l for l in (screen.splitlines() if ok else []) if l.strip()][-8:])
    if "Disconnect this session" not in tail or "Esc to continue" not in tail:
        return False
    sent = send_escape(info["target"])
    log("/remote-control opened its picker in {}; sent Esc: {}".format(info["sid"][:8], sent))
    return sent


def rc_needed(info):
    """True while this session's Remote Control link is down. /remote-control on a connected
    session opens a picker (Disconnect / Show QR / Continue) instead of reconnecting."""
    reg = cn.registry(info["pid"]) or {}
    if reg.get("bridgeSessionId"):
        return False
    st = rc_state(tx(info))
    return not (st and st[0] == "active")


def run_command(info, name, arg, text, after, wanted=lambda: True):
    """Deliver one command and verify it. Returns (result, typed)."""
    target = info["target"]
    verifier = COMMANDS[name][1]
    if name == "remote-control":
        base = wanted
        wanted = lambda: base() and rc_needed(info)  # noqa: E731
    deadline = time.time() + IDLE_WAIT
    detach = False
    try:
        if target[0] == "bg":
            # Attach only once the worker's turn is over, same as compact-now.
            while time.time() < deadline:
                if compact_pending(info["sid"]):
                    return "deferred to compact-now", False
                if not wanted():
                    return "no longer needed", False
                state = cn.session_state(info)
                if state is None:
                    return "background worker identity changed", False
                if state[0] and state[1] / 1000.0 >= (after or 0):
                    break
                time.sleep(1)
            else:
                return "background session never went idle", False
            ok, detail = cn.open_background_pane(info)
            if not ok:
                return detail, False
        offset = tx_size(info)
        outcome = type_when_idle(info, text, after, deadline, wanted)
        if outcome != "typed":
            return outcome, False
        typed_at = time.time()
        detach = True
        end = typed_at + VERIFY_WAIT
        while time.time() < end:
            ok, why = verifier(info, arg, typed_at, offset)
            if ok is not None:
                return ("verified: " + why) if ok else ("failed: " + why), True
            if name == "remote-control" and back_out_of_rc_picker(info, typed_at):
                return "Remote Control was already back; closed its picker with Esc", True
            time.sleep(1)
        return "typed, but nothing confirmed it within {}s".format(VERIFY_WAIT), True
    finally:
        if target[0] == "bg":
            cn.close_background_pane(target, detach=detach)


def _terminate(signum, frame):
    raise SystemExit(128 + signum)  # run the finally blocks: detach bg attach, release, record


def waiter(info, name, arg, text, after, wanted, lock_fd):
    """Runs holding the session's flock (lock_fd), which it releases by exiting."""
    sid = info["sid"]
    result, typed = "crashed", False
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _terminate)
    try:
        result, typed = run_command(info, name, arg, text, after, wanted)
    except SystemExit:
        result = "stopped by a signal"
    except Exception as e:  # noqa: BLE001 - detached, the log is the only place to report
        result = "waiter crashed: {!r}".format(e)
    finally:
        log("/{} for {} ({}): {}".format(name, sid[:8], info.get("name") or "", result))
        try:
            record(sid, name, result, typed)
        except OSError:
            pass
        try:
            os.remove(info_path(sid))
        except OSError:
            pass
        os.close(lock_fd)
    return result, typed


def launch(info, name, arg, text, after, wanted=lambda: True, detach_proc=True):
    """Take the session's flock here, then hand it to a detached waiter (the fd survives the fork,
    so nothing can slip in between). Returns an exit code."""
    sid = info["sid"]
    reason = busy_reason(sid)
    if reason:
        say(reason)
        return 4
    if COMMANDS[name][2](info, arg):
        print("already set: {} needs nothing".format(text) if name != "remote-control" else
              "Remote Control is already connected in this session; nothing to do")
        return 0
    if over_limit(sid, name):
        say("/{} already ran the maximum number of times this hour for this session".format(name))
        return 4
    fd = acquire(sid, name)
    if fd is None:
        say(busy_reason(sid) or "another self-command is queued for this session")
        return 4
    if not detach_proc:
        result, _ = waiter(info, name, arg, text, after, wanted, fd)
        print(result)
        return 0 if result.startswith("verified") or result.startswith("Remote Control was already") else 1
    if cn.daemonize():
        try:
            cn.write_json(info_path(sid), {"command": name, "ts": time.time(), "waiter": os.getpid()})
            waiter(info, name, arg, text, after, wanted, fd)
        finally:
            os._exit(0)
    os.close(fd)  # the detached waiter keeps its own copy of the locked descriptor
    return 0


# ---- entry points --------------------------------------------------------------------------

def called_from_main_thread(transcript):
    """Same test as compact-now: an unanswered main-thread Bash call that runs this script."""
    answered = set()
    for rec in cn.tail_records(transcript, 1 << 20):
        if rec.get("isSidechain"):
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            if rec.get("type") == "user" and (rec.get("origin") or {}).get("kind") == "human":
                return False
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                answered.add(block.get("tool_use_id"))
            elif block.get("type") == "tool_use" and block.get("name") == "Bash" \
                    and block.get("id") not in answered:
                if "self-command" in str((block.get("input") or {}).get("command", "")):
                    return True
    return False


def parsed_or_exit(words):
    try:
        return parse_command(words), 0
    except PermissionError as e:
        say(str(e))
        return None, 6
    except ValueError as e:
        say(str(e))
        return None, 2


def cmd_self(words, dry):
    parsed, code = parsed_or_exit(words)
    if not parsed:
        return code
    name, arg, text = parsed
    sid, pid = os.environ.get("CLAUDE_CODE_SESSION_ID", ""), os.environ.get("CLAUDE_PID", "")
    if not sid or not pid:
        say("run this from a Claude Code session, or pass --pid to drive another one.")
        return 3
    info, why = resolve(pid, sid, from_inside=True)
    if not info:
        say(why)
        return 3
    deadline = time.time() + 3
    while not (info["transcript"] and called_from_main_thread(info["transcript"])):
        if time.time() > deadline:
            say("refused: this isn't the main session's own tool call.")
            return 5
        time.sleep(0.3)
        info["transcript"] = info["transcript"] or cn.find_transcript(sid)
    if dry:
        print("dry run: would type {!r} into {} after this turn ends".format(text, info["target"][0]))
        return 0
    code = launch(info, name, arg, text, time.time())
    if code == 0 and not COMMANDS[name][2](info, arg):
        print("Queued: {} will be typed into this session once this turn ends, then verified. "
              "End the turn now.".format(text))
    return code


def cmd_pid(pid, words, dry, wait):
    parsed, code = parsed_or_exit(words)
    if not parsed:
        return code
    name, arg, text = parsed
    info, why = resolve(pid)
    if not info:
        say(why)
        return 3
    if dry:
        print("dry run: would type {!r} into pid {} ({}, {})".format(text, pid, info["target"][0], info.get("name")))
        return 0
    return launch(info, name, arg, text, 0, detach_proc=not wait)


def live_registries():
    for path in glob.glob(os.path.join(cn.config_dir(), "sessions", "*.json")):
        reg = read_json(path, None)
        if isinstance(reg, dict) and reg.get("pid") and reg.get("sessionId"):
            try:
                os.kill(int(reg["pid"]), 0)
            except ProcessLookupError:
                continue
            except (PermissionError, ValueError, TypeError):
                pass
            yield reg


def rc_attempts_path(sid):
    return os.path.join(STATE_DIR, sid + ".rc-rearm.json")


def rc_candidates(now=None):
    """[(reg, transcript, disconnect_uuid)] for live sessions whose newest Remote Control event is
    an account-change disconnect from the last day."""
    now = now or time.time()
    out = []
    for reg in live_registries():
        if reg.get("spare") or reg.get("kind") not in ("bg", "interactive") or reg.get("bridgeSessionId"):
            continue
        transcript = cn.find_transcript(reg["sessionId"])
        st = rc_state(transcript) if transcript else None
        if st and st[0] == "account" and now - st[1] < RC_LOOKBACK:
            out.append((reg, transcript, st[2]))
    return out


def rc_should_try(sid, key, now=None):
    now = now or time.time()
    att = read_json(rc_attempts_path(sid), {})
    rec = att.get(key) if isinstance(att, dict) else None
    if not rec:
        return True, ""
    typed, fails, waits = rec.get("typed", 0), rec.get("fails", 0), rec.get("waits", 0)
    if typed >= RC_MAX_TYPED:
        return False, "already typed /remote-control {} times for this disconnect".format(typed)
    if fails >= RC_MAX_FAILS:
        return False, "gave up after {} transport failures".format(fails)
    backoff = max(min(600 * fails, 3600), 300 * typed, min(120 * waits, 900))
    if backoff and now - rec.get("last", 0) < backoff:
        return False, "backing off ({}s)".format(int(backoff))
    return True, ""


def rc_note(sid, key, kind):
    """kind: "typed", "fails" (transport problem) or "waits" (busy, draft, deferred)."""
    att = read_json(rc_attempts_path(sid), {})
    att = {k: v for k, v in (att if isinstance(att, dict) else {}).items()
           if isinstance(v, dict) and time.time() - v.get("last", 0) < RC_LOOKBACK}
    rec = att.setdefault(key, {"typed": 0, "fails": 0, "waits": 0})
    rec[kind] = rec.get(kind, 0) + 1
    rec["last"] = time.time()
    cn.write_json(rc_attempts_path(sid), att)


def rc_outcome_kind(result, typed):
    if typed:
        return "typed"
    return "waits" if any(result.startswith(w) for w in WAIT_OUTCOMES) else "fails"


_SKIP_LOGGED = set()


def rc_sweep(dry=False, quiet=False):
    """One pass. Returns the number of sessions a waiter was started for."""
    started = 0
    for reg, transcript, key in rc_candidates():
        sid, pid = reg["sessionId"], reg["pid"]
        label = "{} ({})".format(reg.get("name") or sid[:8], pid)

        def skip(why, note=None):
            if not quiet:
                print("skip {}: {}".format(label, why))
            if note and not dry:
                rc_note(sid, key, note)
            if (sid, key, why) not in _SKIP_LOGGED:
                _SKIP_LOGGED.add((sid, key, why))
                log("rc-sweep skip {}: {}".format(label, why))

        ok, why = rc_should_try(sid, key)
        if not ok:
            if not quiet:
                print("skip {}: {}".format(label, why))
            continue
        reason = busy_reason(sid)
        if reason:
            if not quiet:
                print("skip {}: {}".format(label, reason))
            continue
        if over_limit(sid, "remote-control"):
            skip("hourly /remote-control limit reached")
            continue
        info, why = resolve(pid)
        if not info:
            skip(why, "fails")
            continue
        if not rc_needed(info):
            continue
        if dry:
            print("would type /remote-control into {} via {}".format(label, info["target"][0]))
            continue
        fd = acquire(sid, "remote-control")
        if fd is None:
            continue
        # Still needed = the account-change disconnect is still the newest RC event (a newer
        # disconnect gets its own attempt budget on the next sweep).
        wanted = lambda t=transcript, k=key: (rc_state(t) or (None, 0, None))[2] == k  # noqa: E731
        if cn.daemonize():
            try:
                cn.write_json(info_path(sid), {"command": "remote-control", "ts": time.time(), "waiter": os.getpid()})
                result, typed = waiter(info, "remote-control", None, "/remote-control", 0, wanted, fd)
                if result != "no longer needed":
                    rc_note(sid, key, rc_outcome_kind(result, typed))
            finally:
                os._exit(0)
        os.close(fd)
        started += 1
        log("rc-sweep queued /remote-control for {}".format(label))
        if not quiet:
            print("queued /remote-control for {}".format(label))
    return started


def prune_state(now=None):
    """Drop per-session files for sessions that are gone and a day old."""
    now = now or time.time()
    live = {reg["sessionId"] for reg in live_registries()}
    for pattern in ("*.self-command-history.json", "*.rc-rearm.json", "*.self-command.json", "*.self-command.lock"):
        for path in glob.glob(os.path.join(STATE_DIR, pattern)):
            sid = os.path.basename(path).split(".", 1)[0]
            try:
                if sid not in live and now - os.path.getmtime(path) > RC_LOOKBACK and \
                        not (path.endswith(".lock") and lock_held(sid)):
                    os.remove(path)
            except OSError:
                pass


def reap_orphan_attach_servers():
    """A waiter killed with SIGKILL can't detach its private `claude attach` tmux server. Its
    socket name carries the owning pid, so remove servers whose owner is gone."""
    base = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", "tmux-{}".format(os.getuid()))
    for path in glob.glob(os.path.join(base, "compact-now-*")):
        parts = os.path.basename(path).split("-")
        try:
            owner = int(parts[2])
            os.kill(owner, 0)
            continue
        except ProcessLookupError:
            pass
        except (IndexError, ValueError, PermissionError):
            continue
        cn.run(["tmux", "-L", os.path.basename(path), "kill-server"], timeout=10)
        try:
            os.remove(path)
        except OSError:
            pass
        log("removed orphaned attach server " + os.path.basename(path))


def _code_mtimes():
    out = []
    for f in (os.path.realpath(__file__), os.path.join(HERE, "compact-now.py")):
        try:
            out.append(os.path.getmtime(f))
        except OSError:
            out.append(None)
    return out


def rc_watch(interval):
    log("rc-watch started (every {}s, pid {})".format(interval, os.getpid()))
    started_code = _code_mtimes()
    last_prune = 0
    while True:
        if _code_mtimes() != started_code:
            if None in _code_mtimes():
                log("rc-watch: its scripts are gone; exiting")
                return 0
            log("rc-watch: code changed on disk; restarting")
            os.execv(sys.executable, [sys.executable, os.path.realpath(__file__), "rc-watch",
                                      "--interval", str(interval)])
        try:
            rc_sweep(quiet=True)
            while True:  # reap the short-lived intermediate children daemonize() leaves
                try:
                    pid_, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid_ == 0:
                    break
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                prune_state()
                reap_orphan_attach_servers()
        except Exception as e:  # noqa: BLE001 - keep watching
            log("rc-watch pass failed: {!r}".format(e))
        time.sleep(interval)


def plist_text(script):
    path = os.path.join(HOME, ".local", "bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    env = "<key>PATH</key><string>{}</string>".format(path)
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env += "<key>CLAUDE_CONFIG_DIR</key><string>{}</string>".format(os.environ["CLAUDE_CONFIG_DIR"])
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{script}</string>
    <string>rc-watch</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>{env}</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardErrorPath</key><string>{state}/self-command-watch.err</string>
</dict>
</plist>
""".format(label=LABEL, script=script, env=env, state=STATE_DIR)


def launchctl(*args):
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, timeout=15)


def install_watch():
    if sys.platform != "darwin":
        say("install-watch sets up a LaunchAgent (macOS). Elsewhere, run `rc-watch` under your own supervisor.")
        return 3
    script = os.path.realpath(__file__)
    if "/plugins/cache/" in script:
        say("refusing to point the LaunchAgent at a plugin cache copy ({}); it's replaced on every plugin "
            "update. Run install-watch from ~/.claude/hooks (install.sh) or a stable checkout.".format(script))
        return 3
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    domain = "gui/{}".format(os.getuid())
    launchctl("bootout", "{}/{}".format(domain, LABEL))
    for _ in range(25):  # bootout returns before launchd is done; bootstrap too soon fails with EIO
        if launchctl("print", "{}/{}".format(domain, LABEL)).returncode != 0:
            break
        time.sleep(0.2)
    with open(PLIST, "w") as f:
        f.write(plist_text(script))
    for attempt in range(3):
        res = launchctl("bootstrap", domain, PLIST)
        if res.returncode == 0:
            print("installed {} -> {}\nlog: {}".format(LABEL, script, os.path.join(STATE_DIR, "self-command.log")))
            return 0
        time.sleep(1 + attempt)
    say("launchctl bootstrap failed: {}".format(res.stderr.strip()))
    return 3


def uninstall_watch():
    if sys.platform == "darwin":
        launchctl("bootout", "gui/{}/{}".format(os.getuid(), LABEL))
    try:
        os.remove(PLIST)
    except OSError:
        pass
    print("removed " + LABEL)
    return 0


def status():
    loaded, script = False, None
    if sys.platform == "darwin":
        loaded = launchctl("print", "gui/{}/{}".format(os.getuid(), LABEL)).returncode == 0
        m = re.search(r"<string>(/[^<]*self-command\.py)</string>", open(PLIST).read()) if os.path.exists(PLIST) else None
        script = m.group(1) if m else None
    print("watcher: {}{}".format("loaded" if loaded else "not loaded",
                                 "" if not script else " -> {}{}".format(script, "" if os.path.exists(script) else " (MISSING)")))
    if not cn:
        print("compact-now.py couldn't be loaded next to this script: {}".format(_CN_ERROR))
        return 3
    print("config dir: {}".format(cn.config_dir()))
    for reg, _, key in rc_candidates():
        ok, why = rc_should_try(reg["sessionId"], key)
        print("needs /remote-control: {} ({}){}".format(reg.get("name") or reg["sessionId"][:8], reg["pid"],
                                                        "" if ok else " [" + why + "]"))
    try:
        with open(cn.LOG) as f:
            tail = f.readlines()[-8:]
        print("recent log:\n" + "".join("  " + l for l in tail), end="")
    except OSError:
        pass
    return 0


def main(argv):
    dry = "--dry-run" in argv
    wait = "--wait" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--wait")]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "uninstall-watch":
        return uninstall_watch()
    if argv[0] == "status":
        return status()
    if not cn:
        say("compact-now.py must sit next to this script ({}): {}".format(HERE, _CN_ERROR))
        return 0 if argv[0] == "rc-watch" else 3  # exit 0 stops the LaunchAgent's KeepAlive
    if argv[0] == "rc-sweep":
        rc_sweep(dry=dry)
        return 0
    if argv[0] == "rc-watch":
        interval = 15
        if "--interval" in argv:
            try:
                interval = max(5, int(argv[argv.index("--interval") + 1]))
            except (IndexError, ValueError):
                say("--interval takes a number of seconds")
                return 2
        return rc_watch(interval) or 0
    if argv[0] == "install-watch":
        return install_watch()
    if argv[0] == "--pid":
        if len(argv) < 3:
            say("usage: --pid <pid> <command> [args]")
            return 2
        return cmd_pid(argv[1], argv[2:], dry, wait)
    return cmd_self(argv, dry)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

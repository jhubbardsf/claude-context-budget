"""self-command.py: allowlist, argument checks, Remote Control detection and the rc sweep.

Offline: transcripts and session registries are synthetic files under a temp CLAUDE_CONFIG_DIR,
and nothing is typed anywhere (the sweep runs with dry_run, the transport is never reached).
The live transport paths (bg attach, tmux pane, self mode) were verified by hand on 2026-09-30.
"""
import sys as _s
_s.dont_write_bytecode = True
import importlib.util
import json
import os
import tempfile
import time
import sys
import shutil
import subprocess
import unittest.mock
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "sc", os.path.join(HERE, "..", "plugins", "context-budget", "scripts", "self-command.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
_LOGDIR = tempfile.mkdtemp()
m.cn.LOG = os.path.join(_LOGDIR, "self-command.log")  # never write test noise into the real log

ACCOUNT = ("Remote Control disconnected — signed-in claude.ai account or organization changed on this "
           "machine — run /remote-control to start a session for the current account, or /login to "
           "switch back, then /remote-control")
NETWORK = "Remote Control disconnected — could not reach the Remote Control server for about 30 minutes"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def sysrec(subtype, content, t, uuid):
    return {"type": "system", "subtype": subtype, "content": content, "timestamp": iso(t), "uuid": uuid,
            "isMeta": False}


def user(text, t):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": iso(t),
            "origin": {"kind": "human"}}


def write_transcript(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")


class Parse(unittest.TestCase):
    def test_allowlist_and_alias(self):
        self.assertEqual(m.parse_command(["rc"]), ("remote-control", None, "/remote-control"))
        self.assertEqual(m.parse_command(["color green"]), ("color", "green", "/color green"))
        self.assertEqual(m.parse_command(["/color", "green"]), ("color", "green", "/color green"))
        self.assertEqual(m.parse_command(["rename", "TB3", "-", "done"]), ("rename", "TB3 - done", "/rename TB3 - done"))

    def test_deny_list_wins(self):
        for bad in ("clear", "/exit", "login", "logout", "compact", "permissions", "mcp", "model"):
            with self.assertRaises(PermissionError):
                m.parse_command([bad])

    def test_unknown_command_refused(self):
        with self.assertRaises(PermissionError):
            m.parse_command(["cost"])

    def test_denied_command_never_reaches_allowlist(self):
        with patch.dict(m.COMMANDS, {"clear": (None, m.verify_rc, m.already_rc, (1, 60))}):
            with self.assertRaises(PermissionError):
                m.parse_command(["clear"])

    def test_bad_arguments(self):
        for words in (["rename", "a\nb"], ["rename", "x" * 81], ["rename", "/clear"], ["color", "mauve"],
                      ["remote-control", "now"], ["rename"], ["rename", "foo\\"], ["rename", "see @README"],
                      ["rename", "trailing "]):
            with self.assertRaises(ValueError, msg=words):
                m.parse_command(words)


class RemoteControlState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        m._RC_INC.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_account_change_is_newest(self):
        now = time.time()
        write_transcript(self.path, [sysrec("bridge_status", "/remote-control is active · x", now - 100, "a"),
                                     user("hi", now - 50), sysrec("informational", ACCOUNT, now - 10, "b")])
        self.assertEqual(m.rc_state(self.path)[0::2], ("account", "b"))

    def test_reconnected_after_disconnect(self):
        now = time.time()
        write_transcript(self.path, [sysrec("informational", ACCOUNT, now - 100, "a"),
                                     sysrec("bridge_status", "/remote-control is active · x", now - 5, "b")])
        self.assertEqual(m.rc_state(self.path)[0], "active")

    def test_other_disconnect_is_not_account(self):
        write_transcript(self.path, [sysrec("informational", NETWORK, time.time(), "a")])
        self.assertEqual(m.rc_state(self.path)[0], "other")

    def test_quoted_text_in_other_records_is_ignored(self):
        # An assistant message that quotes the notice (like this very test's author did) must not count.
        now = time.time()
        write_transcript(self.path, [sysrec("bridge_status", "/remote-control is active · x", now - 100, "a"),
                                     {"type": "assistant", "timestamp": iso(now), "message": {"content": [
                                         {"type": "text", "text": '"type":"system" ' + ACCOUNT}]}}])
        self.assertEqual(m.rc_state(self.path)[0], "active")

    def test_since_bound(self):
        now = time.time()
        write_transcript(self.path, [sysrec("informational", ACCOUNT, now - 100, "a")])
        self.assertIsNone(m.rc_state(self.path, since=now - 10))

    def test_cache_invalidates_on_append(self):
        now = time.time()
        write_transcript(self.path, [sysrec("informational", ACCOUNT, now - 100, "a")])
        self.assertEqual(m.rc_state(self.path)[0], "account")
        with open(self.path, "a") as f:
            f.write(json.dumps(sysrec("bridge_status", "/remote-control is active", now, "b"),
                               separators=(",", ":")) + "\n")
        self.assertEqual(m.rc_state(self.path)[0], "active")


class Sweep(unittest.TestCase):
    """rc_candidates / rc_should_try against a fake config dir with live-pid registries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = self.tmp.name
        self.state = os.path.join(self.cfg, "state")
        os.makedirs(os.path.join(self.cfg, "sessions"))
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg})
        self.env.start()
        self.sd = patch.multiple(m, STATE_DIR=self.state)
        self.sd.start()
        self.cnsd = patch.object(m.cn, "STATE_DIR", self.state)
        self.cnsd.start()
        m._RC_INC.clear()

    def tearDown(self):
        self.cnsd.stop()
        self.sd.stop()
        self.env.stop()
        self.tmp.cleanup()

    def session(self, sid, records, kind="bg", pid=None, **extra):
        pid = pid or os.getpid()
        reg = dict({"pid": pid, "sessionId": sid, "kind": kind, "status": "idle", "procStart": "x",
                    "name": sid}, **extra)
        with open(os.path.join(self.cfg, "sessions", "{}.json".format(sid)), "w") as f:
            json.dump(reg, f)
        write_transcript(os.path.join(self.cfg, "projects", "p", sid + ".jsonl"), records)

    def test_only_account_disconnects_are_candidates(self):
        now = time.time()
        self.session("s-account", [sysrec("informational", ACCOUNT, now - 5, "d1")])
        self.session("s-network", [sysrec("informational", NETWORK, now - 5, "d2")])
        self.session("s-active", [sysrec("informational", ACCOUNT, now - 50, "d3"),
                                  sysrec("bridge_status", "/remote-control is active", now - 5, "b3")])
        self.session("s-old", [sysrec("informational", ACCOUNT, now - 3 * 86400, "d4")])
        self.session("s-spare", [sysrec("informational", ACCOUNT, now - 5, "d5")], spare=True)
        self.session("s-sdk", [sysrec("informational", ACCOUNT, now - 5, "d6")], kind="sdk")
        self.session("s-dead", [sysrec("informational", ACCOUNT, now - 5, "d7")], pid=999999)
        got = sorted(reg["sessionId"] for reg, _, _ in m.rc_candidates(now))
        self.assertEqual(got, ["s-account"])

    def test_attempt_limits_and_backoff(self):
        sid, key = "s1", "d1"
        now = time.time()
        self.assertTrue(m.rc_should_try(sid, key)[0])
        m.rc_note(sid, key, "fails")
        ok, why = m.rc_should_try(sid, key)
        self.assertFalse(ok)
        self.assertIn("backing off", why)
        self.assertTrue(m.rc_should_try(sid, key, now=now + 601)[0])
        m.rc_note(sid, key, "typed")
        self.assertFalse(m.rc_should_try(sid, key, now=time.time() + 100)[0])  # typed attempts back off too
        m.rc_note(sid, key, "typed")
        ok, why = m.rc_should_try(sid, key, now=time.time() + 99999)
        self.assertFalse(ok)
        self.assertIn("already typed", why)
        # a NEW disconnect (another account switch) gets a fresh budget
        self.assertTrue(m.rc_should_try(sid, "d2")[0])

    def test_waiting_outcomes_never_give_up(self):
        sid, key = "s1", "d1"
        for _ in range(20):
            m.rc_note(sid, key, m.rc_outcome_kind("never went idle", False))
        ok, why = m.rc_should_try(sid, key, now=time.time() + 901)
        self.assertTrue(ok, why)
        self.assertEqual(m.rc_outcome_kind("pane unreachable", False), "fails")
        self.assertEqual(m.rc_outcome_kind("deferred to compact-now", False), "waits")

    def test_rc_needed_respects_live_bridge(self):
        now = time.time()
        self.session("s1", [sysrec("informational", ACCOUNT, now - 5, "d1")], bridgeSessionId="session_x")
        info = {"pid": str(os.getpid()), "sid": "s1", "transcript": None}
        with patch.object(m.cn, "registry", return_value={"bridgeSessionId": "session_x"}):
            self.assertFalse(m.rc_needed(info))
        with patch.object(m.cn, "registry", return_value={}):
            self.assertTrue(m.rc_needed(info))

    def test_sweep_dry_run_types_nothing(self):
        now = time.time()
        self.session("s-account", [sysrec("informational", ACCOUNT, now - 5, "d1")])
        fake = ({"pid": str(os.getpid()), "sid": "s-account", "proc_start": "x",
                 "target": ["bg", {"job_id": "abc"}, None], "transcript": None, "name": "s"}, "")
        with patch.object(m, "resolve", return_value=fake), \
                patch.object(m.cn, "daemonize", side_effect=AssertionError("dry run must not fork")), \
                patch.object(m.cn, "registry", return_value={}):
            self.assertEqual(m.rc_sweep(dry=True, quiet=True), 0)

    def test_busy_when_compact_pending_or_locked(self):
        os.makedirs(self.state, exist_ok=True)
        self.assertIsNone(m.busy_reason("s1"))
        m.cn.write_json(m.cn.marker_path("s1"), {"ts": time.time()})
        self.assertIn("compact-now", m.busy_reason("s1"))
        os.remove(m.cn.marker_path("s1"))
        fd = m.acquire("s1", "color")
        self.assertIsNotNone(fd)
        self.assertIn("already queued", m.busy_reason("s1"))
        self.assertIsNone(m.acquire("s1", "rename"))  # flock: a second holder can't get in
        self.assertTrue(m.cn.self_command_active("s1"))  # compact-now sees it too
        os.close(fd)
        self.assertIsNone(m.busy_reason("s1"))
        self.assertFalse(m.cn.self_command_active("s1"))

    def test_lock_survives_fork_and_dies_with_holder(self):
        os.makedirs(self.state, exist_ok=True)
        fd = m.acquire("s2", "rename")
        pid = os.fork()
        if pid == 0:  # the "waiter": keeps the inherited descriptor briefly, then exits
            time.sleep(0.5)
            os._exit(0)
        os.close(fd)  # the parent lets go, like launch() does after daemonize
        self.assertTrue(m.lock_held("s2"))
        os.waitpid(pid, 0)
        self.assertFalse(m.lock_held("s2"))

    def test_rate_limit(self):
        os.makedirs(self.state, exist_ok=True)
        for _ in range(m.COMMANDS["rename"][3][0]):
            m.record("s1", "rename", "verified", typed=True)
        self.assertTrue(m.over_limit("s1", "rename"))
        self.assertFalse(m.over_limit("s1", "color"))


class Immediate(unittest.TestCase):
    """/rename runs mid-turn, so it waits only for an empty box, and it never presses Enter on a box
    that holds anything but the command. Someone typing at that moment shares the box."""
    TEXT = "/rename X"
    INFO = {"sid": "s1", "target": ["bg", {"pane": "%1"}, None], "pid": 1, "transcript": ""}

    def run_loop(self, immediate=True, idle=False, draft="", suffix="", pending=False, clears_after=None, readable=True):
        """A fake box: `draft` arrives after the empty reads, `suffix` during the wait before Enter."""
        st = {"box": "", "sent": [], "reads": 0, "typed": 0}

        def render():
            rule = "─" * 100
            return "{}\n❯ {}\n{}\n".format(rule, st["box"], rule)

        def pane(_target, mode, text=""):
            if mode == "read":
                st["reads"] += 1
                if clears_after and st["reads"] == clears_after + 1:
                    st["box"] = ""
                return (True, render()) if readable or not st["typed"] else (True, "no box here")
            st["sent"].append(text)
            return True, "typed"

        def tmux(_target, *args):
            if args[0] == "send-keys":
                keys = list(args[3:])
                if keys[0] == "-l":
                    first = keys[1] == self.TEXT and not st["typed"]
                    st["box"] = (draft if first else "") + st["box"] + keys[1] + (suffix if first else "")
                    st["typed"] += 1
                else:
                    for k in keys:
                        if k == "BSpace":
                            st["box"] = st["box"][:-1]
                        elif k == "Enter":
                            st["sent"].append(st["box"])
                            st["box"] = ""
                return unittest.mock.Mock(returncode=0, stdout="")
            return unittest.mock.Mock(returncode=0, stdout=render())

        with patch.object(m, "compact_pending", return_value=pending), \
                patch.object(m.cn, "session_state", return_value=(idle, 1000)), \
                patch.object(m.cn, "pane", side_effect=pane), \
                patch.object(m.cn, "bg_tmux", side_effect=tmux), \
                patch.object(m.cn, "DRAFT_TIMEOUT", 0.05), \
                patch.object(m.time, "sleep"):
            out = m.type_when_idle(dict(self.INFO), self.TEXT, 0, time.time() + 0.3, lambda: True, immediate)
        return out, st

    def test_only_rename_on_a_checkable_pane_is_immediate(self):
        self.assertTrue(m.is_immediate("rename", ["bg", {}, None]))
        self.assertTrue(m.is_immediate("rename", ["tmux", "%1", None]))
        self.assertFalse(m.is_immediate("rename", ["iterm", "x", "/dev/ttys1"]))
        self.assertFalse(m.is_immediate("color", ["bg", {}, None]))

    def test_empty_box_types_into_a_busy_session(self):
        out, st = self.run_loop()
        self.assertEqual((out, st["sent"], st["box"]), ("typed", [self.TEXT], ""))

    def test_draft_that_arrives_first_is_never_sent(self):
        out, st = self.run_loop(draft="Whc")
        self.assertEqual(st["sent"], [])
        self.assertEqual(st["box"], "Whc")
        self.assertEqual(out, "prompt box held a draft")

    def test_text_typed_during_the_wait_is_kept(self):
        out, st = self.run_loop(suffix="abc")
        self.assertEqual(st["sent"], [])
        self.assertEqual(st["box"], "abc")

    def test_unreadable_box_takes_the_command_back(self):
        _out, st = self.run_loop(readable=False)
        self.assertEqual(st["sent"], [])
        self.assertEqual(st["box"], "")

    def test_sends_once_the_draft_is_gone(self):
        out, st = self.run_loop(draft="Whc", clears_after=4)
        self.assertEqual((out, st["sent"]), ("typed", [self.TEXT]))

    def test_others_still_wait_for_idle(self):
        out, st = self.run_loop(immediate=False)
        self.assertEqual((out, st["sent"]), ("never went idle", []))
        self.assertEqual(self.run_loop(immediate=False, idle=True)[1]["sent"], [self.TEXT])

    def test_immediate_still_yields_to_compact_now(self):
        self.assertEqual(self.run_loop(pending=True)[0], "deferred to compact-now")

    def run_bg(self, name):
        got = {}

        def typer(_info, _text, _after, _deadline, _wanted, immediate=False):
            got["immediate"] = immediate
            return "typed"

        entry = m.COMMANDS[name]
        commands = {name: (entry[0], lambda *a: (True, "confirmed"), entry[2], entry[3])}
        with patch.dict(m.COMMANDS, commands), \
                patch.object(m, "IDLE_WAIT", 0.1), \
                patch.object(m, "compact_pending", return_value=False), \
                patch.object(m, "tx_size", return_value=0), \
                patch.object(m, "type_when_idle", side_effect=typer), \
                patch.object(m.cn, "session_state", return_value=(False, 1000)), \
                patch.object(m.cn, "open_background_pane", return_value=(True, "ok")) as attach, \
                patch.object(m.cn, "close_background_pane"), \
                patch.object(m.time, "sleep"):
            out = m.run_command(dict(self.INFO), name, "X", "/" + name + " X", 0)
        return out, got, attach.call_count

    def test_background_attach_does_not_wait_for_rename(self):
        out, got, attaches = self.run_bg("rename")
        self.assertEqual(out, ("verified: confirmed", True))
        self.assertTrue(got["immediate"])
        self.assertEqual(attaches, 1)

    def test_background_attach_still_waits_for_color(self):
        out, _got, attaches = self.run_bg("color")
        self.assertEqual(out, ("background session never went idle", False))
        self.assertEqual(attaches, 0)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class RealTerminal(unittest.TestCase):
    """type_checked against a real tmux pane running a small stand-in for the prompt box."""
    FAKE = """
import os, sys, tty
tty.setraw(0)
box = ""
def draw():
    rule = "\\u2500" * 100
    sys.stdout.write("\\x1b[2J\\x1b[H" + rule + "\\r\\n\\u276f " + box + "\\r\\n" + rule + "\\r\\n")
    sys.stdout.flush()
draw()
while True:
    for ch in os.read(0, 4096).decode():
        if ch == "\\r":
            open(sys.argv[1], "a").write(box + "\\n")
            box = ""
        elif ch == "\\x7f":
            box = box[:-1]
        else:
            box += ch
    draw()
"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sent = os.path.join(self.dir, "sent")
        fake = os.path.join(self.dir, "fake.py")
        with open(fake, "w") as f:
            f.write(self.FAKE)
        self.env = patch.dict(os.environ, {"TMUX_TMPDIR": self.dir, "LC_ALL": "en_US.UTF-8", "PYTHONIOENCODING": "utf-8"})
        self.env.start()
        os.environ.pop("TMUX", None)
        subprocess.run(["tmux", "-u", "new-session", "-d", "-s", "t", "-x", "120", "-y", "20",
                        "{} {} {}".format(sys.executable, fake, self.sent)], check=True)
        self.pane = subprocess.run(["tmux", "list-panes", "-t", "t", "-F", "#{pane_id}"],
                                   check=True, capture_output=True, text=True).stdout.strip()
        self.target = ["tmux", self.pane, None]
        time.sleep(0.5)

    def tearDown(self):
        subprocess.run(["tmux", "kill-server"], capture_output=True)
        self.env.stop()

    def box(self):
        ok, screen = m.cn.pane(self.target, "read")
        self.assertTrue(ok)
        return m.box_text(screen)

    def sent_lines(self):
        return open(self.sent).read().splitlines() if os.path.exists(self.sent) else []

    def test_clean_box_sends_the_command(self):
        self.assertEqual(m.type_checked(self.target, "/rename X Y")[0], "typed")
        time.sleep(0.3)
        self.assertEqual(self.sent_lines(), ["/rename X Y"])
        self.assertEqual(self.box(), "")

    def test_a_draft_stays_and_nothing_is_sent(self):
        m.send_keys(self.target, ["Whc"], literal=True)
        time.sleep(0.2)
        self.assertEqual(m.type_checked(self.target, "/rename X Y")[0], "draft")
        time.sleep(0.3)
        self.assertEqual(self.sent_lines(), [])
        self.assertEqual(self.box(), "Whc")


class PromptBox(unittest.TestCase):
    W = 120

    def frame(self, box):
        rule = "─" * self.W
        top = "─" * (self.W - 12) + " sc-test ─"
        return "\n".join(["⏺ earlier output", top, box, rule, "  footer"])

    def test_placeholder_counts_as_empty(self):
        self.assertTrue(m.box_empty(self.frame('❯ Try "refactor reference_engine.py"')))

    def test_real_draft_is_not_empty(self):
        self.assertFalse(m.box_empty(self.frame("❯ fix the flaky test please")))

    def test_empty_box(self):
        self.assertTrue(m.box_empty(self.frame("❯ ")))


class Picker(unittest.TestCase):
    PICKER = ("   Remote Control\n   This session is available ...\n     Disconnect this session\n"
              "     Show QR code\n   ❯ Continue\n   Enter to select · Esc to continue\n")

    def run_backout(self, reg, screen=None, acted=False):
        info = {"pid": "1", "sid": "s1abcdef", "target": ["tmux", "%0", "/dev/ttys1"], "transcript": "/x"}
        with patch.object(m.cn, "registry", return_value=dict({"sessionId": "s1abcdef"}, **reg)), \
                patch.object(m.cn, "pane", return_value=(True, screen or self.PICKER)), \
                patch.object(m.cn, "user_acted_since", return_value=acted), \
                patch.object(m, "send_escape", return_value=True) as esc:
            got = m.back_out_of_rc_picker(info, 1000.0)
        return got, esc.call_count

    def test_backs_out_of_our_own_picker(self):
        self.assertEqual(self.run_backout({"status": "waiting", "statusUpdatedAt": 1001000}), (True, 1))

    def test_leaves_other_prompts_alone(self):
        waiting = {"status": "waiting", "statusUpdatedAt": 1001000}
        self.assertEqual(self.run_backout(dict(waiting, waitingFor="permission")), (False, 0))
        self.assertEqual(self.run_backout({"status": "busy", "statusUpdatedAt": 1001000}), (False, 0))
        self.assertEqual(self.run_backout({"status": "waiting", "statusUpdatedAt": 1100000}), (False, 0))  # too late
        self.assertEqual(self.run_backout(waiting, acted=True), (False, 0))  # the user typed since
        self.assertEqual(self.run_backout(waiting, screen="  Allow this?\n  Esc to cancel\n"), (False, 0))


class Verify(unittest.TestCase):
    def test_rename_and_color_need_a_record_written_after_typing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            write_transcript(path, [{"type": "custom-title", "customTitle": "X"},
                                    {"type": "agent-color", "agentColor": "red"}])
            info = {"sid": "s", "pid": "1", "transcript": path}
            offset = os.path.getsize(path)
            self.assertEqual(m.verify_rename(info, "X", 0, offset), (None, ""))
            self.assertEqual(m.verify_color(info, "red", 0, offset), (None, ""))
            with open(path, "a") as f:
                f.write('{"type":"custom-title","customTitle":"X"}\n{"type":"agent-color","agentColor":"red"}\n')
            self.assertTrue(m.verify_rename(info, "X", 0, offset)[0])
            self.assertTrue(m.verify_color(info, "red", 0, offset)[0])


class VerifyRC(unittest.TestCase):
    def test_only_records_after_typing_count(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            now = time.time()
            write_transcript(path, [sysrec("informational", ACCOUNT, now, "d1")])  # just before typing
            info = {"sid": "s", "pid": "1", "transcript": path}
            offset = os.path.getsize(path)
            with patch.object(m.cn, "registry", return_value={}):
                self.assertEqual(m.verify_rc(info, None, now, offset), (None, ""))
                with open(path, "a") as f:
                    f.write(json.dumps(sysrec("bridge_status", "/remote-control is active · x", now + 1, "b"),
                                       separators=(",", ":")) + "\n")
                self.assertTrue(m.verify_rc(info, None, now, offset)[0])
            with patch.object(m.cn, "registry", return_value={"bridgeSessionId": "session_y"}):
                self.assertTrue(m.verify_rc(info, None, now, os.path.getsize(path))[0])


class Ghost(unittest.TestCase):
    def test_dim_text_is_ghost(self):
        self.assertTrue(m.ghost_only("\x1b[39m❯ \x1b[2mTry \"refactor x.py\"\x1b[22m"))
        self.assertTrue(m.ghost_only("❯ \x1b[38;5;244msuggested next prompt\x1b[39m"))

    def test_plain_text_is_a_draft(self):
        self.assertFalse(m.ghost_only("❯ fix the flaky test"))
        self.assertFalse(m.ghost_only("❯ \x1b[2mhint\x1b[22m and typed"))

    def test_empty_box_is_not_ghost(self):
        self.assertFalse(m.ghost_only("❯ "))


class Incremental(unittest.TestCase):
    def test_partial_line_is_read_once_complete(self):
        m._RC_INC.clear()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            now = time.time()
            write_transcript(path, [sysrec("informational", ACCOUNT, now - 10, "d1")])
            self.assertEqual(m.rc_state(path)[0], "account")
            line = json.dumps(sysrec("bridge_status", "/remote-control is active", now, "b1"), separators=(",", ":"))
            with open(path, "a") as f:
                f.write(line[:30])  # half-written record
            self.assertEqual(m.rc_state(path)[0], "account")
            with open(path, "a") as f:
                f.write(line[30:] + "\n")
            self.assertEqual(m.rc_state(path)[0], "active")


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=0).result
    print("ALL PASS" if res.wasSuccessful() else "FAILURES")
    sys.exit(0 if res.wasSuccessful() else 1)

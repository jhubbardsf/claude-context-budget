"""Native-bg transport regressions. No real Claude sessions are opened."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("cn_bg", Path(__file__).resolve().parents[1] / "plugins/context-budget/scripts/compact-now.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class BackgroundTests(unittest.TestCase):
    def test_bg_target_uses_registry_even_with_stale_iterm_environment(self):
        reg = {"kind": "bg", "jobId": "abcdef12", "pid": 42, "sessionId": "full-session"}
        with patch.dict(os.environ, {"ITERM_SESSION_ID": "stale:iterm"}), patch.object(m.shutil, "which", side_effect=lambda x: "/bin/" + x):
            target = m.background_target("42", "full-session", reg)
        self.assertEqual(target[0], "bg")
        self.assertEqual(target[1]["job_id"], "abcdef12")

    def test_target_refuses_identity_mismatch_and_missing_dependency(self):
        reg = {"kind": "bg", "jobId": "abcdef12", "pid": 42, "sessionId": "full-session"}
        with patch.object(m.shutil, "which", return_value="/bin/tool"):
            self.assertIsNone(m.background_target("43", "full-session", reg))
            self.assertIsNone(m.background_target("42", "other-session", reg))
            self.assertIsNone(m.background_target("42", "full-session", dict(reg, kind="interactive")))
        with patch.object(m.shutil, "which", return_value=None):
            self.assertIsNone(m.background_target("42", "full-session", reg))

    def test_bg_hook_leaves_marker_for_owning_waiter(self):
        info = {"target": ["bg", {"job_id": "abcdef12"}, None], "ts": time.time(), "continue": True, "typed_at": time.time()}
        with tempfile.TemporaryDirectory() as d, patch.object(m, "STATE_DIR", d), patch.object(m, "daemonize", side_effect=AssertionError("must not fork a second resume worker")):
            m.write_json(m.marker_path("S1"), info)
            self.assertEqual(m.after_compact("S1", "/fake"), 0)
            self.assertEqual(m.read_marker("S1"), info)

    def test_bg_identity_checks_job_id_as_well_as_worker(self):
        info = {"sid": "S1", "pid": 42, "proc_start": "P1", "target": ["bg", {"job_id": "abcdef12"}, None]}
        reg = {"sessionId": "S1", "procStart": "P1", "kind": "bg", "jobId": "wrong", "status": "idle", "statusUpdatedAt": 1000}
        with patch.object(m, "registry", return_value=reg):
            self.assertIsNone(m.session_state(info))

    def test_bg_screen_uses_attach_tty_not_worker_tty(self):
        now = time.time()
        info = {"sid": "S1", "pid": 42, "target": ["bg", {"job_id": "abcdef12"}, "/dev/attach"], "transcript": None}
        screen = "─" * 80 + "\n❯ \n" + "─" * 80
        calls = []
        def pane(target, mode, text=""):
            if mode == "type": calls.append(text)
            return True, screen
        with patch.object(m, "session_state", return_value=(True, (now+1)*1000)), patch.object(m, "pane", side_effect=pane), patch.object(m, "claude_tty", return_value="/dev/worker"), patch.object(m.time, "sleep"):
            self.assertEqual(m.type_at_idle_prompt(info, "/compact", now, now+5, lambda: True), "typed")
        self.assertEqual(calls, ["/compact"])

    def test_no_attach_when_worker_is_gone_or_user_acted(self):
        for state, acted in ((None, False), ((True, time.time()*1000), True)):
            with self.subTest(state=state, acted=acted), tempfile.TemporaryDirectory() as d:
                info = {"sid": "S1", "pid": 42, "ts": time.time()-1, "target": ["bg", {"job_id": "abcdef12"}, None], "transcript": "/fake"}
                with patch.object(m, "STATE_DIR", d), patch.object(m, "LOG", str(Path(d)/"log")), patch.object(m, "session_state", return_value=state), patch.object(m, "user_acted_since", return_value=acted), patch.object(m, "last_boundary", return_value=(0, None)), patch.object(m, "open_background_pane") as attach:
                    m.write_json(m.marker_path("S1"), info)
                    m.background_waiter(info)
                    attach.assert_not_called()
                    self.assertFalse(Path(m.marker_path("S1")).exists())
                    self.assertTrue(Path(d, "S1.compact-failed").exists())

    def test_automatic_boundary_never_sends_continuation(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            info = {"sid": "S1", "pid": 42, "ts": now-1, "continue": True, "target": ["bg", {"job_id": "abcdef12"}, None], "transcript": "/fake"}
            boundary = [0, None]
            def type_command(*args):
                boundary[:] = [time.time(), "auto"]
                return "typed"
            with patch.object(m, "STATE_DIR", d), patch.object(m, "LOG", str(Path(d)/"log")), patch.object(m, "session_state", return_value=(True, now*1000)), patch.object(m, "user_acted_since", return_value=False), patch.object(m, "last_boundary", side_effect=lambda _: tuple(boundary)), patch.object(m, "open_background_pane", return_value=(True, "attached")), patch.object(m, "type_at_idle_prompt", side_effect=type_command) as typing, patch.object(m, "close_background_pane") as close:
                m.write_json(m.marker_path("S1"), info)
                m.background_waiter(info)
                self.assertEqual(typing.call_count, 1)
                close.assert_called_once_with(info["target"], detach=False)
                self.assertIn("not the requested manual", json.loads(Path(d, "S1.compact-failed").read_text())["reason"])

    def test_attach_refuses_other_session_with_same_job_id(self):
        info = {"sid": "S1", "pid": 42, "target": ["bg", {"job_id": "abcdef12"}, None]}
        response = subprocess.CompletedProcess([], 0, json.dumps([{"kind": "background", "id": "abcdef12", "pid": 42, "sessionId": "other"}]), "")
        with patch.object(m, "session_state", return_value=(True, 1)), patch.object(m, "run", return_value=response), patch.object(m, "bg_tmux") as tmux:
            ok, reason = m.open_background_pane(info)
            self.assertFalse(ok)
            self.assertIn("did not confirm", reason)
            tmux.assert_not_called()

    def test_stale_registry_cannot_validate_restarted_worker(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            (Path(d)/"daemon").mkdir()
            reg = {"kind": "bg", "jobId": "abcdef12", "sessionId": "S1", "pid": os.getpid(), "procStart": "P1", "status": "idle", "statusUpdatedAt": 1000}
            Path(d, "daemon/roster.json").write_text(json.dumps({"workers": {"abcdef12": {"sessionId": "S1", "replPid": os.getpid()+1, "replProcStart": "P2"}}}))
            info = {"sid": "S1", "pid": os.getpid(), "proc_start": "P1", "target": ["bg", {"job_id": "abcdef12"}, None]}
            with patch.object(m, "registry", return_value=reg):
                self.assertIsNone(m.session_state(info))

    def test_user_interruption_blocks_later_manual_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            transcript = Path(d, "transcript.jsonl")
            transcript.write_text("")
            info = {"sid": "S1", "pid": 42, "ts": now-1, "continue": True, "target": ["bg", {"job_id": "abcdef12"}, None], "transcript": str(transcript)}
            def type_command(*args):
                stamp = lambda: m.datetime.datetime.now(m.datetime.timezone.utc).isoformat()
                rows = [
                    {"type": "user", "timestamp": stamp(), "origin": {"kind": "human"}, "message": {"content": "<command-name>/compact</command-name>"}},
                    {"type": "user", "timestamp": stamp(), "message": {"content": "[Request interrupted by user]"}},
                    {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(), "compactMetadata": {"trigger": "manual"}},
                ]
                transcript.write_text("\n".join(map(json.dumps, rows))+"\n")
                return "typed"
            with patch.object(m, "STATE_DIR", d), patch.object(m, "LOG", str(Path(d)/"log")), patch.object(m, "session_state", return_value=(True, now*1000)), patch.object(m, "open_background_pane", return_value=(True, "attached")), patch.object(m, "type_at_idle_prompt", side_effect=type_command) as typing, patch.object(m, "close_background_pane") as close:
                m.write_json(m.marker_path("S1"), info)
                m.background_waiter(info)
                self.assertEqual(typing.call_count, 1)
                close.assert_called_once_with(info["target"], detach=False)
                self.assertIn("user acted", json.loads(Path(d, "S1.compact-failed").read_text())["reason"])

    def test_only_one_own_compact_command_is_ignored_even_without_origin(self):
        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d, "transcript.jsonl")
            now = time.time()
            record = {"type": "user", "timestamp": m.datetime.datetime.now(m.datetime.timezone.utc).isoformat(), "message": {"content": "<command-name>/compact</command-name>\n <command-message>compact</command-message>\n <command-args></command-args>"}}
            transcript.write_text(json.dumps(record)+"\n")
            self.assertFalse(m.user_acted_since(str(transcript), now, ignore_compact=True))
            transcript.write_text((json.dumps(record)+"\n")*2)
            self.assertTrue(m.user_acted_since(str(transcript), now, ignore_compact=True))
            for command in ("<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args>preserve X</command-args>", "<command-name>/model</command-name>"):
                other = dict(record, message={"content": command})
                transcript.write_text(json.dumps(record)+"\n"+json.dumps(other)+"\n")
                self.assertTrue(m.user_acted_since(str(transcript), now, ignore_compact=True))

    @unittest.skipUnless(shutil.which("tmux"), "tmux unavailable for real PTY transport test")
    def test_real_tmux_transport_with_fake_claude(self):
        self.exercise_tmux_transport(True)

    @unittest.skipUnless(shutil.which("tmux"), "tmux unavailable for real PTY transport test")
    def test_real_tmux_transport_without_continuation(self):
        self.exercise_tmux_transport(False)

    def exercise_tmux_transport(self, continue_after):
        """Actual PTY/render/input/detach, with a local fake CLI and transcript."""
        with tempfile.TemporaryDirectory(prefix="cbg-test-") as d:
            root = Path(d)
            (root / "bin").mkdir()
            (root / "cfg/sessions").mkdir(parents=True)
            (root / "cfg/daemon").mkdir()
            (root / "state").mkdir()
            now = time.time()
            reg = {"kind": "bg", "jobId": "abcdef12", "sessionId": "S1", "pid": os.getpid(), "procStart": "P1", "status": "idle", "statusUpdatedAt": now*1000}
            regpath = root / "cfg/sessions/{}.json".format(os.getpid())
            regpath.write_text(json.dumps(reg))
            (root/"cfg/daemon/roster.json").write_text(json.dumps({"workers": {"abcdef12": {"sessionId": "S1", "replPid": os.getpid(), "replProcStart": "P1"}}}))
            transcript = root / "transcript.jsonl"
            transcript.write_text("")
            meta = root / "meta.json"
            meta.write_text(json.dumps({"root": d, "regpath": str(regpath), "row": dict(reg, kind="background", id="abcdef12")}))
            cli = root / "bin/claude"
            cli.write_text(FAKE_CLAUDE)
            cli.chmod(0o755)
            info = {"sid": "S1", "pid": os.getpid(), "proc_start": "P1", "ts": now-0.1, "continue": continue_after, "target": ["bg", {"job_id": "abcdef12"}, None], "transcript": str(transcript)}
            env = {"PATH": str(root/"bin") + os.pathsep + os.environ["PATH"], "CLAUDE_CONFIG_DIR": str(root/"cfg"), "CBG_TEST_META": str(meta)}
            with patch.dict(os.environ, env), patch.object(m, "STATE_DIR", str(root/"state")), patch.object(m, "LOG", str(root/"log")), patch.object(m, "TURN_END_TIMEOUT", 15), patch.object(m, "COMPACT_TIMEOUT", 10):
                m.write_json(m.marker_path("S1"), info)
                m.background_waiter(info)
                self.assertFalse(Path(root/"state/S1.compact-failed").exists(), (root/"log").read_text())
                expected = ["/compact", m.CONTINUE_PROMPT] if continue_after else ["/compact"]
                self.assertEqual((root/"commands").read_text().splitlines(), expected)
                self.assertEqual(m.last_boundary(str(transcript))[1], "manual")
                self.assertTrue((root/"detached").exists(), "client did not receive Ctrl+Z detach")
                self.assertFalse(Path(m.marker_path("S1")).exists())
                self.assertNotEqual(m.bg_tmux(info["target"], "has-session").returncode, 0)


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import datetime, json, os, pathlib, sys, termios, tty
meta = json.loads(pathlib.Path(os.environ['CBG_TEST_META']).read_text())
root = pathlib.Path(meta['root'])
if sys.argv[1:] == ['agents', '--json']:
    print(json.dumps([meta['row']]))
    sys.exit(0)
assert sys.argv[1:] == ['attach', 'abcdef12'], sys.argv
assert 'CLAUDECODE' not in os.environ
assert 'CLAUDE_CODE_SESSION_ID' not in os.environ
tty.setraw(0)
def screen():
    sys.stdout.write('\x1b[2J\x1b[H' + '─'*100 + '\r\n❯ \r\n' + '─'*100 + '\r\n')
    sys.stdout.flush()
screen()
buf = b''
while True:
    char = os.read(0, 1)
    if not char: break
    if char == b'\x1a':
        (root/'detached').write_text('yes')
        break
    if char != b'\r':
        buf += char
        continue
    command = buf.decode()
    buf = b''
    with (root/'commands').open('a') as f: f.write(command+'\n')
    if command == '/compact':
        now = datetime.datetime.now(datetime.timezone.utc)
        command_record = {'type': 'user', 'timestamp': now.isoformat(), 'message': {'content': '<command-name>/compact</command-name>\n <command-message>compact</command-message>\n <command-args></command-args>'}}
        boundary = {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': now.isoformat(), 'compactMetadata': {'trigger': 'manual'}}
        with (root/'transcript.jsonl').open('a') as f:
            f.write(json.dumps(command_record)+'\n')
            f.write(json.dumps(boundary)+'\n')
        regpath = pathlib.Path(meta['regpath'])
        reg = json.loads(regpath.read_text())
        reg['statusUpdatedAt'] = (now.timestamp()+0.01)*1000
        tmp = regpath.with_suffix('.tmp')
        tmp.write_text(json.dumps(reg))
        tmp.replace(regpath)
    screen()
'''

if __name__ == "__main__":
    unittest.main()

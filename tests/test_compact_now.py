"""Offline tests for compact-now.py's decision logic. The pane is mocked: nothing is ever typed."""
import sys as _s
_s.dont_write_bytecode = True
import importlib.util, json, os, sys, tempfile, time
HOME = tempfile.mkdtemp(prefix="cnt-"); os.environ["HOME"] = HOME
os.makedirs(HOME + "/.claude/sessions"); os.makedirs(HOME + "/.claude/postcompact/.state")
spec = importlib.util.spec_from_file_location("cn", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "context-budget", "scripts", "compact-now.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert m.HOME == HOME
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  :: " + str(detail)[:300]))
    if not cond: fails.append(name)
def iso(t): return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
def write_t(recs):
    p = HOME + "/t.jsonl"; open(p, "w").write("\n".join(json.dumps(r) for r in recs) + "\n"); return p
T0 = time.time() - 100
base = [
  {"type": "user", "timestamp": iso(T0 - 50), "origin": {"kind": "human"}, "message": {"content": "do the task"}},
  {"type": "assistant", "timestamp": iso(T0 - 5), "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python3 ~/.claude/hooks/compact-now.py --continue"}}]}},
]
after_ok = [
  {"type": "user", "timestamp": iso(T0 + 1), "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "Queued"}]}},
  {"type": "assistant", "timestamp": iso(T0 + 2), "message": {"content": [{"type": "text", "text": "done"}]}},
  {"type": "user", "timestamp": iso(T0 + 3), "isMeta": True, "message": {"content": "Stop hook feedback: ..."}},
]
# user_acted_since
check("UA no human input after", not m.user_acted_since(write_t(base + after_ok), T0))
check("UA human prompt after", m.user_acted_since(write_t(base + after_ok + [{"type": "user", "timestamp": iso(T0 + 9), "origin": {"kind": "human"}, "message": {"content": "wait, stop"}}]), T0))
check("UA interrupt", m.user_acted_since(write_t(base + after_ok + [{"type": "user", "timestamp": iso(T0 + 9), "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}]), T0))
check("UA queued human prompt", m.user_acted_since(write_t(base + after_ok + [{"type": "attachment", "timestamp": iso(T0 + 9), "attachment": {"type": "queued_command", "prompt": "hey", "commandMode": "prompt", "origin": {"kind": "human"}}}]), T0))
check("UA human prompt BEFORE doesn't count", not m.user_acted_since(write_t(base), T0))
check("UA compact artifacts don't count", not m.user_acted_since(write_t(base + after_ok + [
  {"type": "user", "timestamp": iso(T0 + 9), "message": {"content": "<command-name>/compact</command-name>"}},
  {"type": "user", "timestamp": iso(T0 + 9), "isCompactSummary": True, "message": {"content": "This session is being continued"}},
  {"type": "user", "timestamp": iso(T0 + 9), "message": {"content": "<local-command-stdout>Compacted</local-command-stdout>"}}]), T0))
# called_from_main_thread
check("MT unanswered Bash call", m.called_from_main_thread(write_t(base)))
check("MT answered call is not current", not m.called_from_main_thread(write_t(base + after_ok[:1])))
agent_only = base[:1] + [{"type": "assistant", "timestamp": iso(T0), "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Agent", "input": {"prompt": "run compact-now.py"}}]}}]
check("MT Agent prompt mentioning compact-now doesn't count", not m.called_from_main_thread(write_t(agent_only)))
side = base[:1] + [{"type": "assistant", "isSidechain": True, "timestamp": iso(T0), "message": {"content": [{"type": "tool_use", "id": "s1", "name": "Bash", "input": {"command": "python3 compact-now.py"}}]}}]
check("MT sidechain call doesn't count", not m.called_from_main_thread(write_t(side)))
# type_at_idle_prompt with a fake registry + mocked pane
typed = []
screens = {"now": "x\n" + "─" * 90 + " ultracode ─\n❯ \n" + "─" * 102 + "\n  Model"}
def fake_pane(target, mode, text=""):
    if mode == "read": return True, screens["now"]
    if mode == "type": typed.append(text); return True, "typed"
    return True, "probe"
m.pane = fake_pane
m.claude_tty = lambda pid: "/dev/ttysX"
m.DRAFT_TIMEOUT = 2
def reg(status, upd, sid="S1", proc="P1"):
    json.dump({"sessionId": sid, "procStart": proc, "status": status, "statusUpdatedAt": int(upd * 1000)}, open(HOME + "/.claude/sessions/42.json", "w"))
info = {"target": ["iterm", "U", "/dev/ttysX"], "pid": "42", "sid": "S1", "proc_start": "P1", "transcript": write_t(base + after_ok)}
now = time.time()
reg("idle", now + 0.5)
check("TI types at idle, empty box", m.type_at_idle_prompt(info, "/compact", now, now + 20, lambda: True) == "typed" and typed == ["/compact"], typed)
reg("idle", now + 0.5, sid="OTHER")
check("TI session gone on sessionId mismatch", m.type_at_idle_prompt(info, "/compact", now, now + 5, lambda: True) == "session gone")
reg("idle", now + 0.5, proc="P2")
check("TI session gone on procStart mismatch", m.type_at_idle_prompt(info, "/compact", now, now + 5, lambda: True) == "session gone")
reg("idle", now + 0.5); typed.clear()
screens["now"] = "x\n" + "─" * 90 + " ultracode ─\n❯ half typed\n" + "─" * 102 + "\n  Model"
r = m.type_at_idle_prompt(info, "/compact", now, now + 30, lambda: True)
check("TI draft => gives up, types nothing", r == "prompt box held a draft" and not typed, (r, typed))
screens["now"] = "plain zsh\n❯ "
r = m.type_at_idle_prompt(info, "/compact", now, now + 30, lambda: True)
check("TI unrecognized box => gives up, types nothing", r == "prompt box not recognized" and not typed, (r, typed))
screens["now"] = "x\n" + "─" * 90 + " ultracode ─\n❯ \n" + "─" * 102 + "\n  Model"
info2 = dict(info, transcript=write_t(base + after_ok + [{"type": "user", "timestamp": iso(now + 1), "origin": {"kind": "human"}, "message": {"content": "no wait"}}]))
r = m.type_at_idle_prompt(info2, "/compact", now, now + 10, lambda: True)
check("TI user typed => stands down", r == "the user acted" and not typed, (r, typed))
r = m.type_at_idle_prompt(info, "/compact", time.time(), time.time() + 5, lambda: False); check("TI cancelled via wanted()", r == "cancelled" and not typed, (r, typed))
reg("busy", now + 0.5)
r = m.type_at_idle_prompt(info, "/compact", now, time.time() + 3, lambda: True)
check("TI busy until deadline => never went idle", r == "never went idle" and not typed, r)
reg("idle", now - 50)
r = m.type_at_idle_prompt(info, "/compact", now, time.time() + 3, lambda: True)
check("TI idle from BEFORE the request doesn't count", r == "never went idle" and not typed, r)
m.claude_tty = lambda pid: "/dev/ttysOTHER"; reg("idle", now + 0.5)
r = m.type_at_idle_prompt(info, "/compact", now, time.time() + 4, lambda: True)
check("TI tty changed => never types", not typed, (r, typed))
m.claude_tty = lambda pid: "/dev/ttysX"
# drop_marker
m.write_json(m.marker_path("S1"), {"ts": 123.0})
m.drop_marker("S1", 999.0, "x")
check("DM leaves a newer request's marker alone", os.path.exists(m.marker_path("S1")) and not os.path.exists(m.STATE_DIR + "/S1.compact-failed"))
m.drop_marker("S1", 123.0, "prompt box held a draft")
note = json.load(open(m.STATE_DIR + "/S1.compact-failed"))
check("DM removes own marker and leaves the failure note", not os.path.exists(m.marker_path("S1")) and note["reason"] == "prompt box held a draft")
# after_compact gating (paths that return before daemonizing)
m.write_json(m.marker_path("S2"), {"ts": time.time(), "continue": True})
check("AC no typed_at => consumed, no resume", m.after_compact("S2", "/nonexistent") == 0 and not os.path.exists(m.marker_path("S2")))
m.write_json(m.marker_path("S3"), {"ts": time.time() - 4 * 3600, "continue": True, "typed_at": time.time() - 4 * 3600})
check("AC stale marker => consumed, no resume", m.after_compact("S3", "/nonexistent") == 0 and not os.path.exists(m.marker_path("S3")))
m.write_json(m.marker_path("S4"), {"ts": time.time(), "continue": False, "typed_at": time.time()})
check("AC no --continue => consumed, no resume", m.after_compact("S4", "/nonexistent") == 0 and not os.path.exists(m.marker_path("S4")))

# ---- package review: CLAUDE_CONFIG_DIR moves sessions/ and projects/ ----
CFG = tempfile.mkdtemp(prefix="cnt-cfg-"); os.makedirs(CFG + "/sessions"); os.makedirs(CFG + "/projects/-p")
json.dump({"sessionId": "S9", "status": "idle"}, open(CFG + "/sessions/77.json", "w"))
open(CFG + "/projects/-p/S9.jsonl", "w").write("{}\n")
os.environ["CLAUDE_CONFIG_DIR"] = CFG
check("CFG registry read from CLAUDE_CONFIG_DIR", (m.registry("77") or {}).get("sessionId") == "S9")
check("CFG transcript found under CLAUDE_CONFIG_DIR", m.find_transcript("S9") == CFG + "/projects/-p/S9.jsonl")
del os.environ["CLAUDE_CONFIG_DIR"]
check("CFG unset falls back to ~/.claude", m.registry("77") is None)

# ---- package review (linux lens) ----
import subprocess as _sp
os.environ.update({"CLAUDE_CODE_SESSION_ID": "SX", "CLAUDE_PID": "999999"})
check("L1 a pid the Bash call can't see => exit 6 (sandboxed)", m.request([]) == 6)
del os.environ["CLAUDE_CODE_SESSION_ID"], os.environ["CLAUDE_PID"]
code = ("import importlib.util as u,sys; s=u.spec_from_file_location('c', sys.argv[1]); m=u.module_from_spec(s); "
        "s.loader.exec_module(m); print(m.run(['printf', '\\u2500\\u276f']).stdout == '\\u2500\\u276f')")
out = _sp.run([sys.executable, "-c", code, spec.origin], capture_output=True, text=True,
              env=dict(os.environ, LC_ALL="C", LANG="C", PYTHONIOENCODING="utf-8")).stdout.strip()
check("L3 pane text decodes as UTF-8 under LC_ALL=C", out == "True", out)
if sys.platform.startswith("linux") and os.isatty(0):
    check("L2 /proc tty matches os.ttyname on Linux", m.proc_tty(os.getpid()) == os.ttyname(0), (m.proc_tty(os.getpid()), os.ttyname(0)))
print("\nFAILED:" if fails else "\nALL PASS", fails)
sys.exit(1 if fails else 0)

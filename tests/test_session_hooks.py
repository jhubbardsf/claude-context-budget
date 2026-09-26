"""The two SessionStart(compact) hooks, run as Claude Code runs them: JSON on stdin, fake HOME."""
import sys as _s
_s.dont_write_bytecode = True
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "context-budget", "scripts")
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  :: " + str(detail)[:400]))
    if not cond:
        fails.append(name)


def hook(script, home, stdin, scripts_dir=SCRIPTS):
    env = {"HOME": home, "PATH": os.environ["PATH"]}
    r = subprocess.run(["bash", os.path.join(scripts_dir, script)], input=stdin, capture_output=True, text=True, env=env)
    return r.returncode, r.stdout


def mkhome():
    h = tempfile.mkdtemp(prefix="cbs-")
    os.makedirs(h + "/.claude/postcompact/.state")
    return h


def payload(sid="S1", source="compact"):
    return json.dumps({"source": source, "session_id": sid, "transcript_path": "/nonexistent"})


h = mkhome(); hf = h + "/.claude/postcompact/S1.md"; st = h + "/.claude/postcompact/.state/S1.json"
open(hf, "w").write("fresh body"); json.dump({"warn_ts": time.time() - 30}, open(st, "w"))
rc, out = hook("post-compact-resume.sh", h, payload())
check("resume: fresh handoff injected, handoff + state deleted",
      rc == 0 and "fresh body" in out and "Continue the" in out and not os.path.exists(hf) and not os.path.exists(st), out)
h = mkhome(); hf = h + "/.claude/postcompact/S1.md"; open(hf, "w").write("old body")
old = time.time() - 200 * 60; os.utime(hf, (old, old))
rc, out = hook("post-compact-resume.sh", h, payload())
check("resume: a handoff over 180 min old is background only", "background only" in out and "old body" in out, out)
h = mkhome(); hf = h + "/.claude/postcompact/S1.md"; open(hf, "w").write("pre-warn body")
old = time.time() - 20 * 60; os.utime(hf, (old, old))
json.dump({"warn_ts": time.time() - 60}, open(h + "/.claude/postcompact/.state/S1.json", "w"))
rc, out = hook("post-compact-resume.sh", h, payload())
check("resume: a handoff older than the cycle's warning is background only", "background only" in out, out)
h = mkhome(); hf = h + "/.claude/postcompact/S1.md"; open(hf, "w").write("x")
rc, out = hook("post-compact-resume.sh", h, payload(source="startup"))
check("resume: source=startup does nothing", rc == 0 and out == "" and os.path.exists(hf), out)
rc, out = hook("post-compact-resume.sh", h, "not json")
check("resume: garbage stdin is silent", rc == 0 and out == "", out)
rc, out = hook("post-compact-resume.sh", mkhome(), payload())
check("resume: no handoff, no output", rc == 0 and out == "", out)
h = mkhome()
rc, out = hook("compact-mechanism-note.sh", h, payload())
check("note: prints the handoff path and the absolute compact-now path when outside HOME",
      rc == 0 and h + "/.claude/postcompact/S1.md" in out and os.path.abspath(SCRIPTS) + "/compact-now.py" in out, out)
inside = h + "/plugin/scripts"; shutil.copytree(SCRIPTS, inside)
rc, out = hook("compact-mechanism-note.sh", h, payload(), inside)
check("note: abbreviates to ~ when the scripts live under HOME", "`python3 ~/plugin/scripts/compact-now.py" in out, out)
rc, out = hook("compact-mechanism-note.sh", h, json.dumps({"source": "compact"}))
check("note: missing session_id gets a placeholder", "<session_id>.md" in out, out)
rc, out = hook("compact-mechanism-note.sh", h, payload(source="resume"))
check("note: source=resume does nothing", rc == 0 and out == "", out)
print("\nFAILED:" if fails else "\nALL PASS", fails)
sys.exit(1 if fails else 0)

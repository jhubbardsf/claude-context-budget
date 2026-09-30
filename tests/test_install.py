"""Runs install.sh against throwaway config dirs. Fake `claude` and `chezmoi` shims on PATH keep
it away from the real box: nothing outside the temp dirs is read or written."""
import sys as _s
_s.dont_write_bytecode = True
import glob
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
INSTALL = os.path.join(REPO, "install.sh")
SCRIPTS = ["context-budget-guard.py", "compact-now.py", "self-command.py", "post-compact-resume.sh", "compact-mechanism-note.sh"]
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  :: " + str(detail)[:400]))
    if not cond:
        fails.append(name)


def sandbox(plugin_listed=False):
    root = tempfile.mkdtemp(prefix="cbi-")
    home, cfg, shims = root + "/home", root + "/home/.claude", root + "/shims"
    os.makedirs(cfg)
    os.makedirs(shims)
    listing = "  ❯ context-budget@claude-context-budget" if plugin_listed else "Installed plugins:"
    with open(shims + "/claude", "w") as f:
        f.write('#!/bin/sh\ncase "$1" in --version) echo "9.9.9 (Claude Code)";; plugin) echo "{}";; esac\n'.format(listing))
    with open(shims + "/chezmoi", "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    for s in ("claude", "chezmoi"):
        os.chmod(shims + "/" + s, 0o755)
    env = {"HOME": home, "CLAUDE_CONFIG_DIR": cfg, "PATH": shims + ":/usr/bin:/bin:/usr/sbin:/sbin:" +
           os.path.dirname(sys.executable)}
    return home, cfg, env


def run(env, *args):
    r = subprocess.run(["bash", INSTALL] + list(args), env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def settings(cfg):
    with open(cfg + "/settings.json") as f:
        return json.load(f)


def count(cfg, name):
    return sum(name in h.get("command", "") for groups in settings(cfg).get("hooks", {}).values()
               for g in groups for h in g.get("hooks", []))


# 1. fresh install
home, cfg, env = sandbox()
rc, out = run(env)
check("fresh install exits 0", rc == 0, out)
check("fresh install copies all scripts, executable",
      all(os.access(cfg + "/hooks/" + s, os.X_OK) for s in SCRIPTS), out)
check("fresh install registers guard on 3 events + 2 SessionStart hooks",
      count(cfg, "context-budget-guard.py") == 3 and count(cfg, "post-compact-resume.sh") == 1
      and count(cfg, "compact-mechanism-note.sh") == 1, settings(cfg))
check("SessionStart group uses the compact matcher",
      settings(cfg)["hooks"]["SessionStart"][0].get("matcher") == "compact")
# 2. idempotent re-run
rc, out = run(env)
check("re-run is a no-op", rc == 0 and "already up to date" in out and out.count("(unchanged)") == len(SCRIPTS), out)
check("re-run leaves no settings backup", not glob.glob(cfg + "/settings.json.bak-*"))
check("re-run doesn't duplicate registrations", count(cfg, "context-budget-guard.py") == 3)
# 3. existing settings are preserved, and a hand-registered bare path counts as installed
home, cfg, env = sandbox()
existing = {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/other/stop.sh"}]},
                                              {"hooks": [{"type": "command",
                                                          "command": cfg + "/hooks/context-budget-guard.py"}]}],
                                     "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/g.sh"}]}]},
            "zzz": 1}
os.makedirs(cfg, exist_ok=True)
json.dump(existing, open(cfg + "/settings.json", "w"), indent=2)
rc, out = run(env)
s = settings(cfg)
check("keeps unrelated keys in order", list(s)[:3] == ["model", "hooks", "zzz"], list(s))
check("keeps unrelated hooks", count(cfg, "/other/stop.sh") == 1 and count(cfg, "/g.sh") == 1)
check("doesn't re-add a hand-registered Stop guard", len(s["hooks"]["Stop"]) == 2 and count(cfg, "context-budget-guard.py") == 3, s["hooks"]["Stop"])
check("backs up settings before changing it", len(glob.glob(cfg + "/settings.json.bak-*")) == 1)
# 4. dry run writes nothing
home, cfg, env = sandbox()
rc, out = run(env, "--dry-run")
check("dry run touches nothing", rc == 0 and not os.path.exists(cfg + "/settings.json")
      and not os.path.exists(cfg + "/hooks/compact-now.py") and "would" in out, out)
# 5. a changed script is backed up, then replaced
home, cfg, env = sandbox()
run(env)
with open(cfg + "/hooks/compact-now.py", "a") as f:
    f.write("# local edit\n")
rc, out = run(env)
check("changed script is backed up and replaced",
      len(glob.glob(cfg + "/hooks/compact-now.py.bak-*")) == 1 and "# local edit" not in open(cfg + "/hooks/compact-now.py").read(), out)
# 6. uninstall removes only ours
home, cfg, env = sandbox()
json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/other/stop.sh"}]}]}}, open(cfg + "/settings.json", "w"))
run(env)
rc, out = run(env, "--uninstall")
s = settings(cfg)
check("uninstall removes every registration of ours",
      rc == 0 and all(count(cfg, n) == 0 for n in ("context-budget-guard.py", "post-compact-resume.sh", "compact-mechanism-note.sh")), s)
check("uninstall keeps other hooks and prunes emptied events",
      count(cfg, "/other/stop.sh") == 1 and "PostToolBatch" not in s["hooks"] and "SessionStart" not in s["hooks"], s)
check("uninstall moves the scripts aside", not any(os.path.exists(cfg + "/hooks/" + n) for n in SCRIPTS)
      and len(glob.glob(cfg + "/hooks/*.removed-*")) == len(SCRIPTS))
# 7. refuses to double-install next to the plugin
home, cfg, env = sandbox(plugin_listed=True)
rc, out = run(env)
check("refuses when the plugin is installed", rc == 1 and "fire twice" in out and not os.path.exists(cfg + "/settings.json"), out)
rc, out = run(env, "--force")
check("--force installs anyway", rc == 0 and count(cfg, "context-budget-guard.py") == 3, out)

# ---- package review (installer lens) ----
import stat as _st
def mode(p): return _st.S_IMODE(os.stat(p).st_mode)
# F1: 0600 survives install and uninstall; a new settings.json is created 0600
home, cfg, env = sandbox()
json.dump({"x": 1}, open(cfg + "/settings.json", "w")); os.chmod(cfg + "/settings.json", 0o600)
run(env); m1 = mode(cfg + "/settings.json"); run(env, "--uninstall"); m2 = mode(cfg + "/settings.json")
check("F1 settings.json stays 0600 through install and uninstall", m1 == 0o600 and m2 == 0o600, (oct(m1), oct(m2)))
home, cfg, env = sandbox(); run(env)
check("F1 a new settings.json is created 0600", mode(cfg + "/settings.json") == 0o600, oct(mode(cfg + "/settings.json")))
# F2/F3: look-alike and foreign hooks are never touched; stale paths are updated, not duplicated
home, cfg, env = sandbox()
json.dump({"hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "bash /other/my-post-compact-resume.sh"},
                        {"type": "command", "command": 'python3 "/old/cfg/hooks/context-budget-guard.py"'}]}],
    "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "bash /elsewhere/post-compact-resume.sh"}]}]}},
    open(cfg + "/settings.json", "w"))
rc, out = run(env)
s = settings(cfg)
stop_cmds = [h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]]
check("F3 stale guard path is updated in place, not duplicated",
      sum("context-budget-guard.py" in c for c in stop_cmds) == 1 and any(cfg + "/hooks/context-budget-guard.py" in c for c in stop_cmds), stop_cmds)
check("F3 wrong-matcher SessionStart entry doesn't count; a compact group is added",
      any(g.get("matcher") == "compact" and len(g["hooks"]) == 2 for g in s["hooks"]["SessionStart"]), s["hooks"]["SessionStart"])
rc, out = run(env, "--uninstall")
s = settings(cfg); left = [h["command"] for grp in s.get("hooks", {}).values() for g in grp for h in g["hooks"]]
check("F2 uninstall leaves look-alike and foreign-path hooks alone",
      "bash /other/my-post-compact-resume.sh" in left and "bash /elsewhere/post-compact-resume.sh" in left and "left alone" in out
      and not any(cfg + "/hooks" in c for c in left), (left, out))
# F4: a symlinked settings.json stays a symlink and its target gets the hooks
home, cfg, env = sandbox()
real = cfg + "/real-settings.json"; json.dump({"model": "m"}, open(real, "w")); os.symlink(real, cfg + "/settings.json")
run(env)
check("F4 symlink kept, target updated", os.path.islink(cfg + "/settings.json") and "context-budget-guard.py" in open(real).read())
# F5: malformed settings abort before anything is copied; empty and BOM files are fine
home, cfg, env = sandbox(); open(cfg + "/settings.json", "w").write("{ nope")
rc, out = run(env)
check("F5 invalid JSON aborts cleanly with nothing copied", rc != 0 and "not valid JSON" in out and "Traceback" not in out
      and not os.path.exists(cfg + "/hooks/compact-now.py"), out)
home, cfg, env = sandbox(); open(cfg + "/settings.json", "w").write('{"hooks": []}')
rc, out = run(env)
check("F5 hooks that isn't an object aborts cleanly", rc != 0 and "Traceback" not in out, out)
home, cfg, env = sandbox(); open(cfg + "/settings.json", "w").write("")
rc, out = run(env)
check("F5 empty settings.json is treated as {}", rc == 0 and count(cfg, "context-budget-guard.py") == 3, out)
home, cfg, env = sandbox(); open(cfg + "/settings.json", "wb").write(b'\xef\xbb\xbf{"a": "\xc3\xa9"}')
rc, out = run(env)
check("F5/F10 BOM + non-ASCII survive as UTF-8", rc == 0 and settings(cfg)["a"] == "é", out)
# F11: a config dir with a space, $ and a quote still yields commands that run
root = tempfile.mkdtemp(prefix="cbi-q-"); weird = root + '/cfg $x "q"'
home, _, env = sandbox(); env["CLAUDE_CONFIG_DIR"] = weird; os.makedirs(weird)
rc, out = run(env)
cmds = [h["command"] for grp in json.load(open(weird + "/settings.json"))["hooks"].values() for g in grp for h in g["hooks"]]
res = [subprocess.run(["/bin/sh", "-c", c], input="{}", capture_output=True, text=True, env=env).returncode for c in cmds]
check("F11 quoted commands run from an awkward config path", rc == 0 and len(cmds) == 5 and res == [0] * 5, (out, cmds, res))
# F6: run through a symlink, from another dir, with CDPATH exported
home, cfg, env = sandbox(); link = tempfile.mkdtemp(prefix="cbi-l-") + "/cb-install"; os.symlink(INSTALL, link)
env2 = dict(env, CDPATH="/tmp:/usr")
r = subprocess.run(["bash", link, "--dry-run"], env=env2, capture_output=True, text=True, cwd="/")
check("F6 works via a symlink with CDPATH set", r.returncode == 0 and "would" in r.stdout, r.stdout + r.stderr)
# F9: back-to-back runs never overwrite the first backup
home, cfg, env = sandbox(); original = '{\n    "keep":   "my formatting"\n}\n'
open(cfg + "/settings.json", "w").write(original)
run(env); run(env, "--uninstall")
baks = [open(b).read() for b in glob.glob(cfg + "/settings.json.bak-*")]
check("F9 the pre-install original survives in a backup", original in baks and len(baks) == 2, len(baks))
print("\nFAILED:" if fails else "\nALL PASS", fails)
sys.exit(1 if fails else 0)

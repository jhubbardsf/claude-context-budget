"""Black-box tests for context-budget-guard.py: synthetic transcripts, a fake HOME per case."""
import sys as _s
_s.dont_write_bytecode = True
import json, os, subprocess, tempfile, time, sys
G = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "context-budget", "scripts", "context-budget-guard.py")
fails = []
def mkhome(window=800000):
    h = tempfile.mkdtemp(prefix="cbg-")
    os.makedirs(h + "/.claude/postcompact")
    json.dump({"autoCompactWindow": window} if window else {}, open(h + "/.claude/settings.json", "w"))
    return h
def rec(tokens, model="claude-opus-5-5", **extra):
    r = {"type": "assistant", "isSidechain": False, "message": {"model": model, "usage": {
        "input_tokens": 2, "cache_read_input_tokens": tokens - 1002, "cache_creation_input_tokens": 1000, "output_tokens": 50}}}
    r.update(extra); return json.dumps(r, separators=(",", ":"))
def transcript(home, lines):
    p = home + "/t.jsonl"
    open(p, "w").write("\n".join(lines) + "\n"); return p
def run(home, tpath, event, env=None, **extra):
    data = {"hook_event_name": event, "session_id": "sid1", "transcript_path": tpath, "cwd": home}
    data.update(extra)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("ITERM_SESSION_ID", "TMUX", "CLAUDE_CODE_AUTO", "DISABLE_", "CLAUDE_COMPACT", "CLAUDE_CONTEXT", "CLAUDE_AUTOCOMPACT", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR"))}
    e["HOME"] = home; e.update(env or {})
    r = subprocess.run(["python3", G], input=json.dumps(data), capture_output=True, text=True, env=e)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None
def state(home):
    p = home + "/.claude/postcompact/.state/sid1.json"
    return json.load(open(p)) if os.path.exists(p) else None
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  :: " + str(detail)[:600]))
    if not cond: fails.append(name)

h = mkhome()
t = transcript(h, ["{}", rec(500000)])
check("T1 under line: silent, no state", run(h, t, "PostToolBatch") is None and state(h) is None)
t = transcript(h, [rec(500000), rec(705000)])
o = run(h, t, "PostToolBatch")
check("T2 warn fires at 70.5%", o and o["hookEventName"] == "PostToolBatch" and "70.5%" in o["additionalContext"] and "767,000" in o["additionalContext"] and "no terminal pane" in o["additionalContext"], o)
check("T2 state", state(h) and state(h)["fired"] == ["warn"])
t = transcript(h, [rec(710000)])
check("T3 warn once", run(h, t, "PostToolBatch") is None)
t = transcript(h, [rec(740000)])
o = run(h, t, "PostToolBatch")
check("T4 urgent at 740K", o and "one or two large tool results away" in o["additionalContext"] and "26,950 from here" in o["additionalContext"], o)
t = transcript(h, [rec(745000)])
check("T5 urgent once", run(h, t, "PostToolBatch") is None)
o = run(h, t, "Stop", stop_hook_active=False)
check("T6 stop nudge, no handoff, no pane", o and o["hookEventName"] == "Stop" and "hasn't been written" in o["additionalContext"] and "Then end the turn." in o["additionalContext"], o)
check("T7 stop nudge once", run(h, t, "Stop") is None)
t = transcript(h, [rec(200000)])
check("T8 drop resets state", run(h, t, "PostToolBatch") is None and state(h) is None)
t = transcript(h, [rec(750000)])
o = run(h, t, "UserPromptSubmit")
check("T9 straight to urgent on prompt", o and o["hookEventName"] == "UserPromptSubmit" and "one or two" in o["additionalContext"] and sorted(state(h)["fired"]) == ["urgent", "warn"], o)
h2 = mkhome(); t2 = transcript(h2, [rec(750000)])
check("T10 subagent skipped", run(h2, t2, "PostToolBatch", agent_id="abc") is None and state(h2) is None)
check("T11 stop_hook_active skipped", run(h2, t2, "Stop", stop_hook_active=True) is None)
# T12 fresh handoff: no pane -> silent; with pane -> compact nudge
h3 = mkhome(); t3 = transcript(h3, [rec(710000)])
run(h3, t3, "PostToolBatch"); open(h3 + "/.claude/postcompact/sid1.md", "w").write("handoff")
check("T12a stop, fresh handoff, no pane: silent", run(h3, t3, "Stop") is None)
h4 = mkhome(); t4 = transcript(h4, [rec(710000)])
o = run(h4, t4, "PostToolBatch", env={"ITERM_SESSION_ID": "w0t0p0:ABC"})
check("T12b warn mentions compact-now when pane", o and "compact-now.py --continue" in o["additionalContext"], o)
open(h4 + "/.claude/postcompact/sid1.md", "w").write("handoff")
o = run(h4, t4, "Stop", env={"ITERM_SESSION_ID": "w0t0p0:ABC"})
check("T12c stop, fresh handoff, pane: compact nudge only", o and "natural stopping point" in o["additionalContext"] and "hasn't been written" not in o["additionalContext"], o)
# T12d compact already requested -> silent
h5 = mkhome(); t5 = transcript(h5, [rec(710000)])
os.makedirs(h5 + "/.claude/postcompact/.state"); open(h5 + "/.claude/postcompact/.state/sid1.compact-requested", "w").write("{}")
check("T12d stop silent when compact requested", run(h5, t5, "Stop", env={"ITERM_SESSION_ID": "w0t0p0:ABC"}) is None)
# T13 compact_boundary after the last usage record
h6 = mkhome()
t6 = transcript(h6, [rec(760000), json.dumps({"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 767000, "postTokens": 30000}})])
os.makedirs(h6 + "/.claude/postcompact/.state"); json.dump({"fired": ["warn"]}, open(h6 + "/.claude/postcompact/.state/sid1.json", "w"))
check("T13 boundary => reset", run(h6, t6, "PostToolBatch") is None and state(h6) is None)
# T14 newest records are sidechain / synthetic / zero-usage / tool-result text mentioning usage
h7 = mkhome()
zero = json.dumps({"type": "assistant", "message": {"model": "claude-opus-5", "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
toolres = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": '{"type":"system","subtype":"compact_boundary"} "usage"'}]}})
t7 = transcript(h7, [rec(720000), rec(900000, isSidechain=True), rec(900000, model="<synthetic>"), zero, toolres])
o = run(h7, t7, "PostToolBatch")
check("T14 skips sidechain/synthetic/zero/escaped text", o and "72.0%" in o["additionalContext"], o)
# T15 smaller window via env
h8 = mkhome(); t8 = transcript(h8, [rec(520000)])
o = run(h8, t8, "PostToolBatch", env={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "600000"})
check("T15 window 600K => trigger 567K, warn at 507K", o and "567,000" in o["additionalContext"], o)
# T15b smaller window via settings, below 70% line
h8b = mkhome(window=600000); t8b = transcript(h8b, [rec(520000)])
o = run(h8b, t8b, "PostToolBatch")
check("T15b settings window 600K warns below the 70% line", o and "567,000" in o["additionalContext"], o)
# T16 auto-compact disabled
h9 = mkhome(); t9 = transcript(h9, [rec(710000)])
o = run(h9, t9, "PostToolBatch", env={"DISABLE_AUTO_COMPACT": "1"})
check("T16 auto off => hard limit 977K", o and "977,000" in o["additionalContext"] and "OFF" in o["additionalContext"], o)
h9b = mkhome(); t9b = transcript(h9b, [rec(710000)]); json.dump({"autoCompactEnabled": False}, open(h9b + "/.claude.json", "w"))
o = run(h9b, t9b, "PostToolBatch")
check("T16b ~/.claude.json autoCompactEnabled false", o and "977,000" in o["additionalContext"], o)
# T17 haiku window
h10 = mkhome(window=None); t10 = transcript(h10, [rec(150000, model="claude-haiku-4-5")])
o = run(h10, t10, "PostToolBatch")
check("T17 haiku 200K window: 75% warns, trigger 167K", o and "200,000" in o["additionalContext"] and "167,000" in o["additionalContext"], o)
# T18 garbage input
r = subprocess.run(["python3", G], input="not json", capture_output=True, text=True)
check("T18 garbage stdin fails open", r.returncode == 0 and r.stdout == "", r)
# T19 no usage records at all
h11 = mkhome(); t11 = transcript(h11, ['{"type":"user"}'])
check("T19 no usage => silent", run(h11, t11, "PostToolBatch") is None)
# T20 PCT override lowers trigger
h12 = mkhome(); t12 = transcript(h12, [rec(570000)])
o = run(h12, t12, "PostToolBatch", env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"})
check("T20 pct override 80 => trigger 624K", o and "624,000" in o["additionalContext"], o)

# ---- regression cases from the adversarial review ----
# G-1: auto-compact off only in ~/.claude.json must warn ONCE between 737K and 947K
h13 = mkhome(); json.dump({"autoCompactEnabled": False}, open(h13 + "/.claude.json", "w"))
t13 = transcript(h13, [rec(740000)])
o1 = run(h13, t13, "PostToolBatch"); o2 = run(h13, t13, "PostToolBatch")
check("G1 legacy-off config warns once, with hard-limit wording", o1 and "OFF" in o1["additionalContext"] and o2 is None, (o1, o2))
# G-2: an explicit settings true outranks a stale ~/.claude.json false
h14 = mkhome(); s = json.load(open(h14 + "/.claude/settings.json")); s["autoCompactEnabled"] = True
json.dump(s, open(h14 + "/.claude/settings.json", "w")); json.dump({"autoCompactEnabled": False}, open(h14 + "/.claude.json", "w"))
o = run(h14, transcript(h14, [rec(710000)]), "PostToolBatch")
check("G2 settings true beats ~/.claude.json false", o and "767,000" in o["additionalContext"] and "OFF" not in o["additionalContext"], o)
# G-3: output tokens are counted (rec adds 50)
h15 = mkhome(); o = run(h15, transcript(h15, [rec(699980)]), "PostToolBatch")
check("G3 output tokens push 699,980 inputs over 700K", o and "700,030" in o["additionalContext"], o)
# G-4: a queued compact-now silences PostToolBatch warnings (but still records the tier)
h16 = mkhome(); os.makedirs(h16 + "/.claude/postcompact/.state"); open(h16 + "/.claude/postcompact/.state/sid1.compact-requested", "w").write("{}")
check("G4 queued request silences the warning", run(h16, transcript(h16, [rec(710000)]), "PostToolBatch") is None and state(h16)["fired"] == ["warn"])
# G-4b: a marker older than 45 min no longer suppresses
h17 = mkhome(); os.makedirs(h17 + "/.claude/postcompact/.state"); mp = h17 + "/.claude/postcompact/.state/sid1.compact-requested"
open(mp, "w").write("{}"); old = time.time() - 3600; os.utime(mp, (old, old))
check("G4b stale marker doesn't suppress", run(h17, transcript(h17, [rec(710000)]), "PostToolBatch") is not None)
# G-5: Stop with a handoff older than the refusal window says refresh
h18 = mkhome(); t18 = transcript(h18, [rec(710000)])
os.makedirs(h18 + "/.claude/postcompact/.state"); json.dump({"fired": ["warn"], "warn_ts": time.time() - 7200}, open(h18 + "/.claude/postcompact/.state/sid1.json", "w"))
hp = h18 + "/.claude/postcompact/sid1.md"; open(hp, "w").write("x"); old = time.time() - 40 * 60; os.utime(hp, (old, old))
o = run(h18, t18, "Stop", env={"ITERM_SESSION_ID": "w0t0p0:ABC"})
check("G5 stale handoff => refresh wording", o and "refresh it now" in o["additionalContext"], o)
# failure note from compact-now is surfaced once, even under the line
h19 = mkhome(); os.makedirs(h19 + "/.claude/postcompact/.state")
json.dump({"ts": 1, "reason": "prompt box held a draft"}, open(h19 + "/.claude/postcompact/.state/sid1.compact-failed", "w"))
t19 = transcript(h19, [rec(300000)])
o = run(h19, t19, "UserPromptSubmit")
check("F-note surfaced once", o and "didn't run (prompt box held a draft)" in o["additionalContext"] and run(h19, t19, "UserPromptSubmit") is None, o)
# G-6: auto off + no pane never claims auto-compact fires and asks the user
h20 = mkhome(); o = run(h20, transcript(h20, [rec(710000)]), "PostToolBatch", env={"DISABLE_AUTO_COMPACT": "1"})
check("G6 auto-off, no pane wording", o and "fires by itself" not in o["additionalContext"] and "needs a /compact" in o["additionalContext"] and "don't ask the user" not in o["additionalContext"], o)

# ---- package review (2026-09-26) ----
# F1: settings live under CLAUDE_CONFIG_DIR, not ~/.claude (split config, e.g. cc-account followers)
h21 = mkhome(window=None); cfg21 = h21 + "/cfg"; os.makedirs(cfg21)
json.dump({"autoCompactWindow": 800000}, open(cfg21 + "/settings.json", "w"))
o = run(h21, transcript(h21, [rec(745000)]), "PostToolBatch", env={"CLAUDE_CONFIG_DIR": cfg21})
check("F1 reads settings from CLAUDE_CONFIG_DIR (767K trigger, urgent tier)", o and "767,000" in o["additionalContext"] and "one or two large tool results" in o["additionalContext"], o)
json.dump({"autoCompactEnabled": False}, open(cfg21 + "/.claude.json", "w"))
os.remove(h21 + "/.claude/postcompact/.state/sid1.json")
o = run(h21, transcript(h21, [rec(745000)]), "PostToolBatch", env={"CLAUDE_CONFIG_DIR": cfg21})
check("F1 reads .claude.json from CLAUDE_CONFIG_DIR", o and "OFF" in o["additionalContext"], o)
# F2: a plugin dir that merely shares a prefix with HOME keeps its absolute path
import shutil
root22 = tempfile.mkdtemp(prefix="cbg22-"); h22 = root22 + "/josh"; os.makedirs(h22 + "/.claude/postcompact")
json.dump({"autoCompactWindow": 800000}, open(h22 + "/.claude/settings.json", "w"))
os.makedirs(root22 + "/josh-work/scripts"); g2 = root22 + "/josh-work/scripts/context-budget-guard.py"; shutil.copy(G, g2)
e = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CONFIG_DIR", "TMUX", "CLAUDE_COMPACT"))}
e.update({"HOME": h22, "ITERM_SESSION_ID": "w0t0p0:ABC"})
d = {"hook_event_name": "PostToolBatch", "session_id": "sid1", "transcript_path": transcript(h22, [rec(710000)]), "cwd": h22}
out = subprocess.run(["python3", g2], input=json.dumps(d), capture_output=True, text=True, env=e).stdout
check("F2 no bogus ~-work path", root22 + "/josh-work/scripts/compact-now.py" in out and "~-work" not in out, out[:300])
print("\nFAILED:" if fails else "\nALL PASS", fails)
sys.exit(1 if fails else 0)

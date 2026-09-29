"""Tests for compact-now.py's input-box parser on sanitized copies of real Claude Code layouts.

The layouts mirror what `text of s` (iTerm, whole scrollback) and `tmux capture-pane -p`
(visible rows) returned on 2026-09-26: a top border that can carry a label ("── ultracode ─"
or a session name), older boxes and bare dash lines left in scrollback, and a footer below.
The first parser only accepted unlabelled rules and got every real session wrong.
"""
import sys as _s
_s.dont_write_bytecode = True
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "cn", os.path.join(HERE, "..", "plugins", "context-budget", "scripts", "compact-now.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

W = 180
RULE = "─" * W


def top(label):
    return ("─" * (W - len(label) - 3) + " " + label + " ─") if label else RULE


def frame(label, box_lines, scrollback=()):
    return "\n".join(list(scrollback) + [
        "⏺ Some earlier answer text that isn't part of the input box.",
        "",
        "✻ Worked for 12s · done 2:04 AM",
        "",
        top(label),
    ] + box_lines + [
        RULE,
        "   Model: Opus 5.5  Context: [███░░░░░] 181k/1.0M (18%)",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ])


OLD_BOXES = [
    "❯ an earlier prompt that was already sent",
    "─" * 88,                      # a bare dash line inside some tool output
    top("ultracode"), "❯ ", RULE,   # a whole old input box left in scrollback
    "⏺ more conversation",
]

fails = []


def expect(name, got, want):
    ok = got is want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  :: got {} want {}".format(got, want)))
    if not ok:
        fails.append(name)


# The last one is a real /rename title: 50 chars on a 180-col rule is only ~72% dashes, which the
# first labelled-rule check (>=80% dashes) rejected, so a bg attach never typed (2026-09-29).
for label in ("", "ultracode", "TB3 - Review - Task55", "TB3 - Review - Task58_v2 - Monday.9.28.26.10:06PM"):
    tag = label or "unlabelled"
    for sb_name, sb in (("clean", ()), ("scrollback", OLD_BOXES)):
        expect("{} / {} / empty".format(tag, sb_name), m.prompt_box_empty(frame(label, ["❯ "], sb)), True)
        expect("{} / {} / draft".format(tag, sb_name), m.prompt_box_empty(frame(label, ["❯ so the thing I wanted"], sb)), False)
        expect("{} / {} / 2-line draft".format(tag, sb_name),
               m.prompt_box_empty(frame(label, ["❯ first line", "  second line"], sb)), False)
        visible = "\n".join(frame(label, ["❯ "], sb).split("\n")[-12:])
        expect("{} / {} / visible rows only".format(tag, sb_name), m.prompt_box_empty(visible), True)
expect("plain shell, no box", m.prompt_box_empty("user@host ~\n❯ ls\nfoo\n❯ "), None)
expect("rules but no prompt under the top", m.prompt_box_empty("x\n" + RULE + "\nsomething\n" + RULE), None)
expect("empty screen (tmux copy mode)", m.prompt_box_empty(""), None)
print("\nFAILED:" if fails else "\nALL PASS", fails)
sys.exit(1 if fails else 0)

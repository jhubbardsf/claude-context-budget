#!/usr/bin/env bash
# SessionStart hook (matcher: compact): after a compaction, inject a short
# orientation so the freshly-compacted agent understands the handoff machinery
# even if the docs that describe it got summarized out of context.
#
# Runs on SessionStart because that is the only compaction-adjacent event whose
# stdout is injected into the model context (PreCompact output would be summarized
# away by the very compaction it precedes). Pairs with post-compact-resume.sh,
# which prints/deletes the actual handoff just above this note.
set -u

here=$(cd "$(dirname "$0")" && pwd)
input=$(cat)
read -r src sid < <(printf '%s' "$input" | python3 -c 'import json,sys
try: d = json.load(sys.stdin)
except Exception: d = {}
print(d.get("source") or "-", d.get("session_id") or "<session_id>")')
[ "$src" = "compact" ] || exit 0

hf="${HOME}/.claude/postcompact/${sid}.md"
cn="${here}/compact-now.py"
case "$cn" in "$HOME"/*) cn="~${cn#"$HOME"}" ;; esac

cat <<EOF
[compact mechanism] You just resumed after a compaction. How the handoff works:
- Any handoff written before this compaction lived at ${hf} and, if present, was injected and deleted just above. Resume from its "next action".
- To checkpoint before a FUTURE compaction, write the handoff to ${hf} (a stable path, NOT a repo path, because CLAUDE_PROJECT_DIR can go stale if a worktree is removed mid-session). The context-budget hook warns you mid-turn at 70% of the window and again ~30K tokens before auto-compact, which fires by itself even in the middle of a task (at 767K with autoCompactWindow 800000, ~967K by default on 1M models). Don't ask the user about compacting.
- To compact on your own terms at a clean checkpoint, make \`python3 ${cn} --continue\` your last tool call and end the turn: it types /compact into this session's pane once the turn is over, then a resume prompt afterwards (drop --continue when you're done and waiting on the user). Main thread only; it refuses without a fresh handoff, exits 3 in sessions with no pane, and stands down if the user presses Esc or types. If it gives up, the context-budget hook tells you.
EOF
exit 0

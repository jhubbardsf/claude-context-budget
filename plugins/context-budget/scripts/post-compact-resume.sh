#!/usr/bin/env bash
# SessionStart hook (matcher: compact): after a compaction (manual /compact or
# auto), reload the handoff the agent wrote before compacting, then delete it.
# stdout from a SessionStart hook is injected into the model's context, so this
# is what makes the agent read the handoff and resume.
#
# Also resets context-budget-guard.py's per-session state so its warnings can arm
# again this session, and, when the compaction was requested by the model with
# `compact-now.py --continue`, hands off to compact-now.py, which waits for the
# compaction to finish and then types a "continue from the handoff" prompt.
#
# Handoff location: a STABLE per-session file, ~/.claude/postcompact/<session_id>.md.
# It doesn't depend on CLAUDE_PROJECT_DIR, which is pinned at session launch and
# goes stale if a worktree is removed mid-session. A handoff that predates this
# context cycle's warning, or is hours old, is still injected but labelled as
# background rather than as the thing to resume.
#
# Needs only bash and python3 (no jq), so it runs unchanged on macOS and Linux.
set -u

here=$(cd "$(dirname "$0")" && pwd)
input=$(cat)
field() {
  printf '%s' "$input" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get(sys.argv[1]) or "")
except Exception: print("")' "$1"
}
[ "$(field source)" = "compact" ] || exit 0
sid=$(field session_id)
transcript=$(field transcript_path)
[ -n "$sid" ] || exit 0

state="${HOME}/.claude/postcompact/.state/${sid}.json"
warn_ts=$(python3 -c 'import json,sys
try: print(int(json.load(open(sys.argv[1])).get("warn_ts") or 0))
except Exception: print(0)' "$state")
rm -f "$state" 2>/dev/null
# Consumes the compact-now marker; detaches before any waiting, so this returns at once.
python3 "${here}/compact-now.py" --after-compact "$sid" "$transcript" </dev/null >/dev/null 2>&1 || true

f="${HOME}/.claude/postcompact/${sid}.md"
[ -f "$f" ] || exit 0
now=$(date +%s)
mtime=$(python3 -c 'import os,sys; print(int(os.path.getmtime(sys.argv[1])))' "$f" 2>/dev/null || echo "$now")
age_min=$(( (now - mtime) / 60 ))
stale=""
if [ "$age_min" -gt 180 ] || { [ "$warn_ts" -gt 0 ] && [ "$mtime" -lt $(( warn_ts - 60 )) ]; }; then
  stale=1
fi

echo "===== RESUMING AFTER COMPACTION ====="
if [ -z "$stale" ]; then
  echo "The handoff below was written ${age_min} min before this compaction. Continue the"
  echo "task from its 'next action'."
else
  echo "The handoff below is ${age_min} min old and predates this context cycle, so it may"
  echo "describe earlier work. The compaction summary above is more current: use this as"
  echo "background only, and overwrite it at your next checkpoint."
fi
echo "This file has already been consumed and deleted; do not look for it on disk."
echo "-------------------------------------"
cat "$f"
echo "-------------------------------------"

rm -f "$f"
exit 0

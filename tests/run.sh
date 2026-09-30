#!/usr/bin/env bash
# Run every test suite. Offline and safe: fake HOMEs, a mocked pane, and shims for claude/chezmoi.
set -u
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1  # keep __pycache__ out of plugins/, which a path install copies
rc=0
logs=$(mktemp -d)
for t in test_guard.py test_compact_now.py test_bg_compact.py test_prompt_box.py test_session_hooks.py test_install.py test_self_command.py; do
  printf '== %s\n' "$t"
  python3 "$t" > "$logs/$t.log" 2>&1 && tail -1 "$logs/$t.log" || { cat "$logs/$t.log"; rc=1; }
done
rm -rf "$logs"
exit $rc

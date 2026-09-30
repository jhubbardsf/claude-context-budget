#!/usr/bin/env bash
# Install context-budget as plain user hooks (no plugin system):
#   copies the scripts into <config>/hooks and registers them in <config>/settings.json.
# <config> is $CLAUDE_CONFIG_DIR if set, else ~/.claude.
#
#   ./install.sh               install or update (idempotent; backs up anything it replaces)
#   ./install.sh --dry-run     show what would change, touch nothing
#   ./install.sh --uninstall   remove our registrations and scripts (handoffs are kept)
#   ./install.sh --check       only report prerequisites
#   ./install.sh --force       install even if the plugin version is also installed
#
# Prefer the plugin install on boxes where you use plugins (see README.md). Don't run both:
# every hook would fire twice.
set -euo pipefail

# Resolve the repo through symlinks (works on macOS without GNU readlink -f), ignoring CDPATH.
self=$0
while [ -L "$self" ]; do
  link=$(readlink "$self")
  case "$link" in /*) self=$link ;; *) self=$(dirname "$self")/$link ;; esac
done
repo=$(CDPATH= cd -- "$(dirname -- "$self")" >/dev/null && pwd -P)
src="$repo/plugins/context-budget/scripts"
merge="$repo/installer/settings_merge.py"
cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
dest="$cfg/hooks"
settings="$cfg/settings.json"
scripts=(context-budget-guard.py compact-now.py self-command.py post-compact-resume.sh compact-mechanism-note.sh)

mode=install dry="" force=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    --uninstall) mode=uninstall ;;
    --check) mode=check ;;
    --force) force=1 ;;
    -h|--help) sed -n '2,13p' "$self"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 64 ;;
  esac
done

say() { printf '%s\n' "$*"; }
run() { if [ -n "$dry" ]; then say "  would: $*"; else "$@"; fi; }
q() { printf '%q' "$1"; }
free() {  # first of $1, $1.1, $1.2 ... that doesn't exist, so a backup never clobbers another
  local cand=$1 n=1
  while [ -e "$cand" ]; do cand="$1.$n"; n=$((n + 1)); done
  printf '%s' "$cand"
}

check() {
  local ok=1
  if ! command -v python3 >/dev/null 2>&1; then
    say "✗ python3 not found (required)"; ok=0
  elif ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    say "✗ python3 is older than 3.9 (required)"; ok=0
  else
    say "✓ python3 $(python3 -c 'import platform; print(platform.python_version())')"
  fi
  if [ "$(uname -s)" = "Darwin" ] && osascript -e 'id of application "iTerm"' >/dev/null 2>&1; then
    say "✓ iTerm2 present: compact-now can type into iTerm panes"
  fi
  if command -v tmux >/dev/null 2>&1; then
    if [ "$(uname -s)" = "Linux" ] && [ ! -r "/proc/$$/stat" ] && ! ps -o tty= -p $$ >/dev/null 2>&1; then
      say "• tmux present, but neither /proc nor 'ps -o tty= -p' works (install procps): compact-now can't find its pane"
    else
      say "✓ tmux present: compact-now can type into tmux panes"
    fi
  elif [ "$(uname -s)" != "Darwin" ]; then
    say "• no tmux: warnings and handoffs work, but compact-now can't type /compact on this box"
  fi
  if command -v claude >/dev/null 2>&1; then
    say "✓ claude $(claude --version 2>/dev/null | head -1)"
  else
    say "• claude CLI not on PATH (fine for install, needed to use it)"
  fi
  [ "$ok" = 1 ]
}

plugin_installed() {
  local list
  command -v claude >/dev/null 2>&1 || return 1
  list=$(claude plugin list 2>/dev/null) || return 1
  case "$list" in *"context-budget@"*) return 0 ;; esac
  return 1
}

chezmoi_hint() {
  command -v chezmoi >/dev/null 2>&1 || return 0
  chezmoi source-path "$settings" >/dev/null 2>&1 || return 0
  local s args=""
  for s in "${scripts[@]}"; do args+=" $(q "$dest/$s")"; done
  say ""
  say "chezmoi manages $(q "$settings"). Capture the change so a later 'chezmoi apply' doesn't undo it:"
  if [ "$mode" = install ]; then
    say "  chezmoi re-add $(q "$settings"); chezmoi add$args"
  else
    say "  chezmoi re-add $(q "$settings"); chezmoi forget --force$args"
  fi
}

case "$mode" in
  check)
    check
    ;;
  install)
    check || { say "fix the prerequisites above first"; exit 1; }
    if plugin_installed && [ -z "$force" ]; then
      say "The context-budget plugin is already installed on this box; installing the hooks too would"
      say "make every hook fire twice. Uninstall the plugin first, or pass --force."
      exit 1
    fi
    python3 "$merge" validate "$settings" >/dev/null || { say "nothing was installed"; exit 1; }
    say ""
    say "Installing into $dest"
    run mkdir -p "$dest"
    stamp=$(date +%Y%m%d-%H%M%S)
    for s in "${scripts[@]}"; do
      if [ -f "$dest/$s" ] && cmp -s "$src/$s" "$dest/$s"; then
        say "  = $s (unchanged)"
        continue
      fi
      [ -f "$dest/$s" ] && run cp -p "$dest/$s" "$(free "$dest/$s.bak-$stamp")"
      run cp "$src/$s" "$dest/$s"
      run chmod +x "$dest/$s"
      say "  + $s"
    done
    python3 "$merge" install "$settings" "$dest" ${dry:+--dry-run}
    run mkdir -p "$HOME/.claude/postcompact/.state"
    say ""
    say "Done. New sessions get everything. Sessions already running pick up the Stop and"
    say "UserPromptSubmit hooks right away; the mid-turn PostToolBatch warning can take a while"
    say "to reach them, so restart a session if you want it there now."
    say "Optional: python3 $(q "$dest/self-command.py") install-watch re-runs /remote-control in"
    say "sessions that lose Remote Control when the signed-in account changes (macOS LaunchAgent)."
    say "A running watcher restarts itself when these scripts change."
    chezmoi_hint
    ;;
  uninstall)
    python3 "$merge" uninstall "$settings" "$dest" ${dry:+--dry-run}
    stamp=$(date +%Y%m%d-%H%M%S)
    for s in "${scripts[@]}"; do
      if [ -f "$dest/$s" ]; then
        to=$(free "$dest/$s.removed-$stamp")
        run mv "$dest/$s" "$to"
        say "  - $s (moved to $(basename "$to"))"
      fi
    done
    # self-command's Remote Control watcher points at the script just moved away; without this its
    # LaunchAgent would retry a missing file at every login.
    rc_plist="$HOME/Library/LaunchAgents/dev.joshuahubbard.cc-self-command-rc.plist"
    if [ -f "$rc_plist" ] && grep -qF "$dest/self-command.py" "$rc_plist"; then
      run launchctl bootout "gui/$(id -u)/dev.joshuahubbard.cc-self-command-rc" 2>/dev/null || true
      run rm -f "$rc_plist"
      say "  - Remote Control watcher (LaunchAgent) removed"
    fi
    say "Handoffs and state in ~/.claude/postcompact are left alone."
    chezmoi_hint
    ;;
esac

#!/usr/bin/env python3
"""Add, check or remove the context-budget hook registrations in a Claude Code settings.json.

    settings_merge.py install   <settings.json> <hooks_dir> [--dry-run]
    settings_merge.py uninstall <settings.json> <hooks_dir> [--dry-run]
    settings_merge.py validate  <settings.json>

A hook is ours only when its script operand resolves to <hooks_dir>/<one of our script
names>, so a user's own `my-post-compact-resume.sh`, or a same-named script elsewhere, is never
touched. Install is idempotent: an entry of ours that's already registered on the right event
(and, for SessionStart, a matcher that covers "compact") is left alone however the command is
spelled, one pointing at a stale path is rewritten in place, and anything missing is added.
Everything else in the file, key order included, is preserved.

Writes are careful: a symlinked settings.json is updated through the link, the file keeps its
permissions (it can hold secrets, so a new one is created 0600), the write is atomic, the
backup never overwrites an earlier one, and everything is UTF-8 whatever the locale.
Python 3.9 compatible.
"""
import json
import os
import re
import shlex
import shutil
import stat
import sys
import time

GUARD = "context-budget-guard.py"
RESUME = "post-compact-resume.sh"
NOTE = "compact-mechanism-note.sh"
OURS = (GUARD, RESUME, NOTE)
INTERPRETERS = ("python3", "python", "bash", "sh", "env")


class Refuse(Exception):
    pass


def canonical(hooks_dir, name):
    interp = "python3" if name.endswith(".py") else "bash"
    return "{} {}".format(interp, shlex.quote(os.path.join(hooks_dir, name)))


def wanted(hooks_dir):
    """(event, matcher, [script names]) that install makes sure exist."""
    return [
        ("PostToolBatch", None, [GUARD]),
        ("UserPromptSubmit", None, [GUARD]),
        ("Stop", None, [GUARD]),
        ("SessionStart", "compact", [RESUME, NOTE]),
    ]


def resolve(path):
    return os.path.realpath(os.path.expanduser(os.path.expandvars(path)))


def operand(command):
    """The script a hook command runs: argv[0], or the first argument after an interpreter
    (skipping its flags, and env's NAME=value assignments)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    while argv and os.path.basename(argv[0]) in INTERPRETERS:
        argv = argv[1:]
        while argv and (argv[0].startswith("-") or "=" in argv[0]):
            argv = argv[1:]
    return argv[0] if argv else None


def ours(hook, hooks_dir):
    """Our script name when this hook runs <hooks_dir>/<name>, else None."""
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return None
    script = operand(hook["command"])
    if not script or os.path.basename(script) not in OURS:
        return None
    return os.path.basename(script) if resolve(script) == resolve(os.path.join(hooks_dir, os.path.basename(script))) else None


def named_like_ours(hook):
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return None
    script = operand(hook["command"])
    return os.path.basename(script) if script and os.path.basename(script) in OURS else None


def matcher_covers(group, value):
    m = group.get("matcher")
    if m in (None, "", "*"):
        return True
    try:
        return re.fullmatch(m, value) is not None or re.search(r"(^|\|)" + re.escape(value) + r"($|\|)", m) is not None
    except re.error:
        return m == value


def groups_of(hooks, event):
    groups = hooks.get(event)
    if groups is None:
        return None
    if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups) or \
            not all(isinstance(g.get("hooks", []), list) for g in groups):
        raise Refuse("hooks.{} isn't a list of hook groups".format(event))
    return groups


def load(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise Refuse("{}: can't read it ({})".format(path, e))
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise Refuse("{}: not valid JSON (line {} col {}); fix or remove it, nothing was changed".format(
            path, e.lineno, e.colno))
    if not isinstance(data, dict):
        raise Refuse("{} isn't a JSON object; nothing was changed".format(path))
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise Refuse("{}: \"hooks\" isn't an object; nothing was changed".format(path))
    return data


def free_name(base):
    """base, or base.1, base.2 ... whichever doesn't exist yet (claimed atomically)."""
    n = 0
    while True:
        cand = base if n == 0 else "{}.{}".format(base, n)
        try:
            os.close(os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return cand
        except FileExistsError:
            n += 1


def save(path, data, dry):
    if dry:
        return
    target = os.path.realpath(path)  # write through a symlink, don't replace it
    folder = os.path.dirname(target) or "."
    os.makedirs(folder, exist_ok=True)
    mode = 0o600
    if os.path.exists(target):
        mode = stat.S_IMODE(os.stat(target).st_mode)
        backup = free_name("{}.bak-{}".format(target, time.strftime("%Y%m%d-%H%M%S")))
        shutil.copy2(target, backup)
    tmp = "{}.tmp-{}".format(target, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install(path, hooks_dir, dry):
    data = load(path)
    hooks = data.setdefault("hooks", {})
    changes = []
    for event, matcher, names in wanted(hooks_dir):
        groups = groups_of(hooks, event)
        if groups is None:
            groups = hooks[event] = []
        missing = []
        for name in names:
            present = False
            for g in groups:
                if matcher and not matcher_covers(g, matcher):
                    continue
                for h in g.get("hooks", []):
                    if ours(h, hooks_dir) == name:
                        present = True
                    elif named_like_ours(h) == name and _looks_installed_by_us(h):
                        h["command"] = canonical(hooks_dir, name)  # a stale path from an older install
                        changes.append("{} -> {} (path updated)".format(event, name))
                        present = True
            if not present:
                missing.append(name)
        if missing:
            entries = [{"type": "command", "command": canonical(hooks_dir, n), "timeout": 15 if n == GUARD else 30}
                       for n in missing]
            groups.append({"matcher": matcher, "hooks": entries} if matcher else {"hooks": entries})
            changes += ["{} -> {}".format(event, n) for n in missing]
    if changes:
        save(path, data, dry)
    return changes


def _looks_installed_by_us(hook):
    """An entry whose script lives in some other `.../hooks/` dir with our exact name, which is
    what an install from an older config dir leaves behind."""
    script = operand(hook.get("command", "")) or ""
    return os.path.basename(os.path.dirname(resolve(script))) == "hooks"


def uninstall(path, hooks_dir, dry):
    data = load(path)
    hooks = data.get("hooks") or {}
    removed, left = [], []
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups, touched = [], False
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                kept_groups.append(g)
                continue
            kept = []
            for h in g["hooks"]:
                name = ours(h, hooks_dir)
                if name:
                    removed.append("{} -> {}".format(event, name))
                    continue
                if named_like_ours(h):
                    left.append("{}: {}".format(event, h.get("command")))
                kept.append(h)
            if len(kept) < len(g["hooks"]):
                touched = True
                if not kept:
                    continue  # a group we emptied goes; groups that were already empty stay
                g["hooks"] = kept
            kept_groups.append(g)
        if touched and not kept_groups:
            del hooks[event]
        else:
            hooks[event] = kept_groups
    if removed:
        save(path, data, dry)
    return removed, left


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    try:
        if len(args) >= 2 and args[0] == "validate":
            data = load(args[1]).get("hooks") or {}
            for event in data:
                groups_of(data, event)
            print("settings: {} is usable".format(args[1]))
            return 0
        if len(args) >= 3 and args[0] == "install":
            changes = install(args[1], args[2], dry)
            left = []
        elif len(args) >= 3 and args[0] == "uninstall":
            changes, left = uninstall(args[1], args[2], dry)
        else:
            sys.stderr.write(__doc__)
            return 64
    except Refuse as e:
        sys.stderr.write("settings: {}\n".format(e))
        return 2
    verb = "would change" if dry else "changed"
    print("settings: {} {} registration(s)".format(verb, len(changes)) if changes else "settings: already up to date")
    for c in changes:
        print("  " + c)
    for l in left:
        print("  left alone (not ours): " + l)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Install or uninstall AIDE judge hooks in ~/.claude/settings.json."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "prototype/judge/"


def prototype_root() -> Path:
    return Path(__file__).resolve().parent.parent


def aide_hooks(root: Path) -> dict:
    py = "python3"
    j = root / "judge"
    cmd = lambda name: f'{py} "{j / name}"'

    return {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("prompt_judge.py"),
                        "timeout": 15,
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash|WebFetch|WebSearch",
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("pretool_gate.py"),
                        "timeout": 5,
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Bash|WebFetch|WebSearch",
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("posttool_record.py"),
                        "timeout": 5,
                    }
                ]
            }
        ],
        "PostToolUseFailure": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("posttool_record.py"),
                        "timeout": 5,
                    }
                ]
            }
        ],
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("session_start.py"),
                        "timeout": 10,
                    }
                ]
            },
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("scheduled_mine.py"),
                        "timeout": 5,
                        "async": True,
                    }
                ]
            },
        ],
        "PreCompact": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("precompact.py"),
                        "timeout": 10,
                    }
                ]
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("stop_verify.py"),
                        "timeout": 10,
                    }
                ]
            }
        ],
    }


def aide_permissions(root: Path) -> list[str]:
    """Allow rules so /aide works without permission prompts in auto mode."""
    j = root / "judge"
    return [
        "Read(~/.claude-judge/**)",
        "Write(~/.claude-judge/feedback/**)",
        "Write(~/.claude-judge/config.json)",
        f'Bash(python3 "{j / "doctor.py"}":*)',
        f"Bash(python3 {j / 'doctor.py'}:*)",
    ]


def merge_permissions(settings: dict, rules: list[str]) -> tuple[dict, int]:
    out = dict(settings)
    perms = dict(out.get("permissions") or {})
    allow = list(perms.get("allow") or [])
    added = 0
    for rule in rules:
        if rule not in allow:
            allow.append(rule)
            added += 1
    perms["allow"] = allow
    out["permissions"] = perms
    return out, added


def remove_permissions(settings: dict) -> tuple[dict, int]:
    out = dict(settings)
    perms = dict(out.get("permissions") or {})
    allow = list(perms.get("allow") or [])
    kept = [r for r in allow
            if ".claude-judge" not in r and "judge/doctor.py" not in r]
    removed = len(allow) - len(kept)
    if kept:
        perms["allow"] = kept
    else:
        perms.pop("allow", None)
    if perms:
        out["permissions"] = perms
    else:
        out.pop("permissions", None)
    return out, removed


def hook_commands(entry: dict) -> list[str]:
    return [
        h.get("command", "")
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    ]


def is_aide_hook_command(command: str) -> bool:
    return MARKER in command and ".py" in command


def merge_event(existing: list, incoming: list) -> tuple[list, int]:
    merged = list(existing)
    added = 0
    existing_cmds = {
        cmd
        for entry in merged
        for cmd in hook_commands(entry)
    }
    for entry in incoming:
        new_cmds = [c for c in hook_commands(entry) if c not in existing_cmds]
        if not new_cmds:
            continue
        merged.append(entry)
        added += len(new_cmds)
        existing_cmds.update(new_cmds)
    return merged, added


def merge_settings(settings: dict, incoming_hooks: dict) -> tuple[dict, int]:
    out = dict(settings)
    hooks = dict(out.get("hooks") or {})
    total_added = 0
    for event, entries in incoming_hooks.items():
        merged, added = merge_event(hooks.get(event, []), entries)
        hooks[event] = merged
        total_added += added
    out["hooks"] = hooks
    return out, total_added


def uninstall_hooks(settings: dict) -> tuple[dict, int]:
    hooks = dict(settings.get("hooks") or {})
    removed = 0
    for event, entries in list(hooks.items()):
        kept_entries = []
        for entry in entries:
            kept_hooks = []
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if is_aide_hook_command(cmd):
                    removed += 1
                    continue
                kept_hooks.append(hook)
            if kept_hooks:
                kept = dict(entry)
                kept["hooks"] = kept_hooks
                kept_entries.append(kept)
        if kept_entries:
            hooks[event] = kept_entries
        else:
            hooks.pop(event, None)
    out = dict(settings)
    out["hooks"] = hooks
    return out, removed


def install_command(root: Path) -> Path:
    src = root / "judge" / "commands" / "aide.md"
    dest_dir = Path.home() / ".claude" / "commands"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "aide.md"
    shutil.copy2(src, dest)
    return dest


def main() -> int:
    if sys.version_info < (3, 9):
        print(f"AIDE needs Python 3.9+ (found {sys.version.split()[0]}).")
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true",
                    help="Remove AIDE hook entries from settings.json")
    args = ap.parse_args()

    root = prototype_root()
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        settings = {}

    backup = None
    if args.uninstall:
        merged, removed = uninstall_hooks(settings)
        merged, perms_removed = remove_permissions(merged)
        removed += perms_removed
        if settings_path.exists() and removed:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = settings_path.with_name(f"settings.json.bak-aide-{stamp}")
            shutil.copy2(settings_path, backup)
            settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        print(f"AIDE prototype: {root}")
        print(f"Settings:       {settings_path}")
        if backup:
            print(f"Backup:         {backup}")
        print(f"Hooks removed:  {removed}")
        return 0

    incoming = aide_hooks(root)
    merged, added = merge_settings(settings, incoming)
    merged, perms_added = merge_permissions(merged, aide_permissions(root))
    added += perms_added

    if settings_path.exists() and added:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = settings_path.with_name(f"settings.json.bak-aide-{stamp}")
        shutil.copy2(settings_path, backup)

    if added:
        settings_path.write_text(
            json.dumps(merged, indent=2) + "\n",
            encoding="utf-8",
        )

    aide_dest = install_command(root)

    print(f"AIDE prototype: {root}")
    print(f"Settings:       {settings_path}")
    if backup:
        print(f"Backup:         {backup}")
    print(f"Hooks added:    {added}")
    print(f"/aide command:  {aide_dest}")
    if added:
        print("Done. Restart Claude Code or open a new session.")
    else:
        print("Already installed. No hook changes needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""AIDE doctor — one-command health check for the installation.

Run directly or via `/aide status`. Prints a short human-readable report:
hooks registered, data directory state, rulebook age, recent activity,
and the LLM backends available for optional features. Exit code 0 = healthy
or degraded-but-working, 1 = not installed / broken.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
MARKER = "judge/"
EXPECTED_HOOKS = {
    "UserPromptSubmit": "prompt_judge.py",
    "PreToolUse": "pretool_gate.py",
    "PostToolUse": "posttool_record.py",
    "PostToolUseFailure": "posttool_record.py",
    "SessionStart": "session_start.py",
    "PreCompact": "precompact.py",
    "Stop": "stop_verify.py",
}

OK, WARN, FAIL = "ok", "warn", "fail"
ICONS = {OK: "✓", WARN: "!", FAIL: "✗"}


def check(status, label, detail=""):
    return {"status": status, "label": label, "detail": detail}


def registered_hook_commands():
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    found = {}
    for event, entries in (settings.get("hooks") or {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if MARKER in cmd and ".py" in cmd:
                    found.setdefault(event, []).append(cmd)
    return found


def check_python():
    if sys.version_info >= (3, 9):
        return check(OK, "Python", f"{sys.version.split()[0]}")
    return check(FAIL, "Python", f"{sys.version.split()[0]}, need 3.9+")


def check_hooks():
    found = registered_hook_commands()
    missing = [e for e in EXPECTED_HOOKS if e not in found]
    if not found:
        return check(FAIL, "Hooks", "none registered. Run install_hooks.py")
    if missing:
        return check(WARN, "Hooks",
                     f"{len(found)}/{len(EXPECTED_HOOKS)} events registered; "
                     f"missing: {', '.join(missing)}")
    stale = [cmds[0] for cmds in found.values()
             if not Path(cmds[0].split('"')[1] if '"' in cmds[0]
                         else cmds[0].split()[-1]).exists()]
    if stale:
        return check(FAIL, "Hooks", "registered but script path missing. "
                                    "Re-run install_hooks.py from the new checkout")
    return check(OK, "Hooks", f"all {len(EXPECTED_HOOKS)} events registered")


def check_data_dir():
    if not DATA_DIR.exists():
        return check(WARN, "Data dir", f"{DATA_DIR} missing, created on first prompt")
    return check(OK, "Data dir", str(DATA_DIR))


def check_rulebook():
    p = DATA_DIR / "rulebook.json"
    if not p.exists():
        return check(WARN, "Rulebook",
                     "none. Tier-1 rules + transforms still active; "
                     "run the miner when you want personalized patterns")
    age_h = (time.time() - p.stat().st_mtime) / 3600
    try:
        rb = json.loads(p.read_text())
        n = len(rb.get("patterns", []))
    except (OSError, json.JSONDecodeError):
        return check(FAIL, "Rulebook", "unreadable JSON. Delete it and re-mine")
    return check(OK, "Rulebook", f"{n} mined patterns, updated {age_h:.0f}h ago")


def check_activity():
    p = DATA_DIR / "telemetry.jsonl"
    if not p.exists():
        return check(WARN, "Activity", "no telemetry yet. Has a session run since install?")
    lines = p.read_text(errors="replace").strip().splitlines()
    if not lines:
        return check(WARN, "Activity", "telemetry empty")
    last = {}
    try:
        last = json.loads(lines[-1])
    except json.JSONDecodeError:
        pass
    age_h = (time.time() - last.get("ts", 0)) / 3600 if last.get("ts") else None
    recent = [json.loads(l) for l in lines[-200:] if l.strip()]
    transforms = sum(1 for r in recent if r.get("channel") == "transform")
    detail = f"{len(lines)} events"
    if age_h is not None:
        detail += f", last {age_h:.1f}h ago"
    detail += f", {transforms} transforms in last {len(recent)}"
    return check(OK, "Activity", detail)


def check_llm_backends():
    cli = shutil.which("claude")
    key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if key:
        return check(OK, "LLM polish", "API key present (fast path)")
    if cli:
        return check(OK, "LLM polish", "claude CLI available (subscription path)")
    return check(WARN, "LLM polish",
                 "no API key or claude CLI, transforms stay deterministic (still fine)")


def check_feedback():
    d = DATA_DIR / "feedback"
    n = len(list(d.glob("*.json"))) if d.exists() else 0
    legacy = DATA_DIR / "feedback.jsonl"
    if legacy.exists():
        n += len(legacy.read_text(errors="replace").strip().splitlines())
    if not n:
        return check(OK, "Feedback", "none recorded. Try /aide feedback <what happened>")
    return check(OK, "Feedback", f"{n} entries in {d}")


def main() -> int:
    checks = [
        check_python(),
        check_hooks(),
        check_data_dir(),
        check_rulebook(),
        check_activity(),
        check_llm_backends(),
        check_feedback(),
    ]
    print("AIDE doctor")
    print("-----------")
    for c in checks:
        print(f" {ICONS[c['status']]} {c['label']:<10} {c['detail']}")
    worst = ("fail" if any(c["status"] == FAIL for c in checks)
             else "warn" if any(c["status"] == WARN for c in checks) else "ok")
    print("-----------")
    if worst == "fail":
        print("Status: BROKEN. Fix the ✗ items above.")
        return 1
    if worst == "warn":
        print("Status: working (some optional pieces inactive).")
        return 0
    print("Status: healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""SessionStart hook — resume recap with R11 dedup."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt_judge import (  # noqa: E402
    DATA_DIR, MARKS_PATH, RULEBOOK_PATH, load_json, read_tail_entries,
    compute_signals,
)

COMPACT_MEMORY_DIR = DATA_DIR / "compact-memory"


def main():
    if os.environ.get("AIDE_JUDGE_BYPASS"):  # optimizer CLI child — no recursion
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd") or os.getcwd()
    entries = read_tail_entries(payload.get("transcript_path"))
    rulebook = load_json(RULEBOOK_PATH, {})
    sig = compute_signals("", entries, cwd, rulebook)

    all_marks = load_json(MARKS_PATH, {})
    marks = all_marks.get(session_id, {})
    thresholds = rulebook.get("thresholds") or {}
    stale_hours = thresholds.get("stale_hours", 12)
    stale_tokens = thresholds.get("stale_carried_tokens", 20_000)

    messages = []
    if (sig["hours_since_last_event"] > stale_hours
            and sig["context_tokens"] > stale_tokens
            and not marks.get("r11_delivered")):
        marks["r11_delivered"] = True
        messages.append(
            f"[judge/resume] Session resumed after ~{sig['hours_since_last_event']:.0f}h "
            f"with ~{sig['context_tokens'] // 1000}k carried tokens. "
            "Consider /clear unless you need the history.")

    try:
        COMPACT_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        snapshots = sorted(COMPACT_MEMORY_DIR.glob("compact-*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if snapshots and snapshots[0].stat().st_mtime > time.time() - 86400:
            messages.append(
                f"[judge/resume] Latest compact snapshot: {snapshots[0]}")
    except OSError:
        pass

    all_marks[session_id] = marks
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MARKS_PATH.write_text(json.dumps(dict(list(all_marks.items())[-50:])))
    except OSError:
        pass

    if messages:
        print(json.dumps({"systemMessage": "\n".join(messages)}))
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Stop hook — one-shot verification nudge when session ends without test evidence."""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt_judge import (  # noqa: E402
    read_tail_entries, compute_signals, load_json, RULEBOOK_PATH,
)
from session_state import intervention_scope, load, save  # noqa: E402

TEST_EVIDENCE_RE = re.compile(
    r"\b(pytest|npm test|npm run test|go test|cargo test|jest|vitest|unittest"
    r"|mvn test|rspec|tox|make test)\b", re.I)


def session_has_test_evidence(entries):
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        items = content if isinstance(content, list) else []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            if item.get("name") != "Bash":
                continue
            cmd = (item.get("input") or {}).get("command", "")
            if TEST_EVIDENCE_RE.search(cmd):
                return True
    return False


def main():
    if os.environ.get("AIDE_JUDGE_BYPASS"):  # optimizer CLI child — no recursion
        sys.exit(0)
    if intervention_scope() != "full":
        sys.exit(0)  # prompt-only scope (default): never block a stop
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        sys.exit(0)

    # Guard: only fire on real stop, not nested stop_hook_active loops.
    if payload.get("stop_hook_active"):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd") or os.getcwd()
    state = load(session_id)
    if state.get("stop_verify_fired"):
        sys.exit(0)

    entries = read_tail_entries(payload.get("transcript_path"))
    rulebook = load_json(RULEBOOK_PATH, {})
    sig = compute_signals("", entries, cwd, rulebook)

    if sig.get("session_has_file_edits") and not session_has_test_evidence(entries):
        state["stop_verify_fired"] = True
        save(session_id, state)
        print(json.dumps({
            "systemMessage": (
                "[judge/stop] This session has file edits but no test/build "
                "evidence yet. Run verification before committing.")
        }))
    sys.exit(0)


if __name__ == "__main__":
    main()

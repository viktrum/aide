#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure recorder for AIDE V2.

Registers for matcher Bash|WebFetch|WebSearch. Always exit 0.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_state import load, save  # noqa: E402

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
TELEMETRY_PATH = DATA_DIR / "telemetry.jsonl"
PAYLOAD_SMOKE_PATH = DATA_DIR / "payload_smoke_posttool.json"

POLL_RE = re.compile(r"\b(sleep|tail\s+-f|watch)\b", re.I)


def _log_payload_smoke(session_id, hook_name, payload):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if PAYLOAD_SMOKE_PATH.exists():
            return
        PAYLOAD_SMOKE_PATH.write_text(json.dumps({
            "session_id": session_id,
            "hook": hook_name,
            "keys": sorted(payload.keys()),
        }, indent=1))
    except OSError:
        pass


def normalize_command(cmd):
    cmd = re.sub(r"\s+", " ", (cmd or "").strip())
    cmd = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=(?:[^\s]+|\S+)\s+", "", cmd)
    return cmd


def stderr_signature(text):
    import hashlib
    t = re.sub(r"\d+", "", (text or ""))[:200]
    return hashlib.sha1(t.encode()).hexdigest()[:12]


def main():
    if os.environ.get("AIDE_JUDGE_BYPASS"):  # optimizer CLI child — no recursion
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    _log_payload_smoke(session_id, payload.get("hook_event_name") or "PostToolUse", payload)
    name = payload.get("tool_name") or ""
    event = payload.get("hook_event_name") or "PostToolUse"
    tin = payload.get("tool_input") or {}

    state = load(session_id)
    state.setdefault("bash", {})

    if name in ("WebFetch", "WebSearch"):
        state["web_chain"] = state.get("web_chain", 0) + 1
    elif name:
        state["web_chain"] = 0

    if name == "Bash":
        cmd = normalize_command(tin.get("command", "") if isinstance(tin, dict) else "")
        if cmd and not POLL_RE.search(cmd):
            import hashlib
            h = hashlib.sha1(cmd.encode()).hexdigest()[:12]
            rec = state["bash"].setdefault(h, {"count": 0, "stderr_sig": "", "cmd": cmd[:120]})
            rec["cmd"] = cmd[:120]
            if event == "PostToolUseFailure":
                sig = stderr_signature(payload.get("error", ""))
                if rec.get("stderr_sig") == sig:
                    rec["count"] = rec.get("count", 0) + 1
                else:
                    rec.update(count=1, stderr_sig=sig)
            else:
                rec["count"] = 0

    save(session_id, state)
    sys.exit(0)


if __name__ == "__main__":
    main()

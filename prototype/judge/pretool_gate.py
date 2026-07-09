#!/usr/bin/env python3
"""PreToolUse gate for AIDE V2 — deny wasteful/risky tool calls."""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_state import load  # noqa: E402
from posttool_record import normalize_command, stderr_signature  # noqa: E402

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
RULEBOOK_PATH = DATA_DIR / "rulebook.json"
TELEMETRY_PATH = DATA_DIR / "telemetry.jsonl"

DESTRUCTIVE_RE = re.compile(
    r"\brm\s+-rf\b|\bgit\s+push\s+.*--force\b|\bgit\s+reset\s+--hard\b|"
    r"\bdrop\s+(database|table|schema)\b|\bcurl\s+[^|]*\|\s*sh\b|"
    r"\bprod(uction)?\s+(db|database)\b",
    re.I)
AUTO_MODES = {"auto", "acceptEdits", "bypassPermissions", "dontAsk"}
PAYLOAD_SMOKE_PATH = DATA_DIR / "payload_smoke.json"


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


def load_rulebook():
    try:
        return json.loads(RULEBOOK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def log_deny(session_id, tool_name, reason):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(TELEMETRY_PATH, "a") as f:
            f.write(json.dumps({
                "ts": round(time.time(), 3),
                "session_id": session_id,
                "hook": "pretool",
                "source": "pretool_gate",
                "channel": "deny",
                "tool": tool_name,
                "reason": reason[:200],
            }) + "\n")
    except OSError:
        pass


def main():
    if os.environ.get("AIDE_JUDGE_BYPASS"):  # optimizer CLI child — no recursion
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        sys.exit(0)

    session_id = payload.get("session_id", "unknown")
    _log_payload_smoke(session_id, "pretool", payload)
    name = payload.get("tool_name") or ""
    tin = payload.get("tool_input") or {}
    if not isinstance(tin, dict):
        tin = {}

    rb = load_rulebook()
    thresholds = rb.get("thresholds") or {}
    web_max = int(thresholds.get("web_chain_min", 12))

    state = load(session_id)

    if name in ("WebFetch", "WebSearch") and not state.get("research_intent"):
        chain = state.get("web_chain", 0)
        if chain >= web_max:
            reason = (f"{chain} consecutive web fetches this turn. Summarize findings "
                      "and check the repo before fetching more. Fetch again only for "
                      "specific missing documentation.")
            log_deny(session_id, name, reason)
            deny(reason)

    if name == "Bash":
        cmd = normalize_command(tin.get("command", ""))
        if cmd:
            import hashlib
            h = hashlib.sha1(cmd.encode()).hexdigest()[:12]
            rec = (state.get("bash") or {}).get(h) or {}
            if rec.get("count", 0) >= 3:
                reason = (f"This exact command failed {rec['count']}x with the same error "
                          f"({rec.get('stderr_sig', '')}). Diagnose environment, working "
                          "directory, dependencies, and script configuration before rerunning.")
                log_deny(session_id, name, reason)
                deny(reason)

        mode = (payload.get("permission_mode") or "").lower()
        if mode in AUTO_MODES and DESTRUCTIVE_RE.search(cmd):
            reason = ("Destructive command under auto-accept. Review manually or switch "
                      "permission mode before continuing.")
            log_deny(session_id, name, reason)
            deny(reason)

    sys.exit(0)


if __name__ == "__main__":
    main()

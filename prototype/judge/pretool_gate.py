#!/usr/bin/env python3
"""PreToolUse gate for AIDE V2 — deny wasteful/risky tool calls."""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_state import intervention_scope, load  # noqa: E402
from posttool_record import normalize_command, stderr_signature  # noqa: E402

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
RULEBOOK_PATH = DATA_DIR / "rulebook.json"
TELEMETRY_PATH = DATA_DIR / "telemetry.jsonl"

AUTO_MODES = {"auto", "acceptedits", "bypasspermissions", "dontask"}

# --- Destructive-command gate: scan commands, not data. ----------------------
# Three tiers under auto-accept:
#   deny  -> catastrophic only (recursive-force delete aimed at root or home)
#   ask   -> ambiguous (rm -rf on absolute/variable/parent paths, hard resets,
#            force pushes, DROPs, curl|sh): one confirmation, no hard block
#   allow -> routine (rm -rf on relative or tmp paths, mentions inside data)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.S | re.M)
INTERPRETER_HEREDOC_RE = re.compile(
    r"\b(?:ba|z|da)?sh\s+(?:-\w+\s+)*<<|\bpython3?\s+(?:-\w+\s+)*<<|\bnode\s+(?:-\w+\s+)*<<")
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
SHELL_EVAL_RE = re.compile(r"\b(?:ba|z|da)?sh\s+-c\b|\beval\b|\bxargs\b|\bsource\s")
ASK_DESTRUCTIVE_RE = re.compile(
    r"\bgit\s+push\s+[^|;&]*--force(-with-lease)?\b|\bgit\s+reset\s+--hard\b|"
    r"\bdrop\s+(database|table|schema)\b|\bcurl\s+[^|]*\|\s*(ba|z|da)?sh\b|"
    r"\bprod(uction)?\s+(db|database)\b",
    re.I)
RM_CMD_RE = re.compile(r"(?:^|[|;&(]\s*)(?:sudo\s+)?rm\s+((?:--?[\w=-]+\s+)+)([^|;&><]*)", re.M)
SAFE_TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
                     "$TMPDIR")


def scan_surface(cmd):
    """Reduce a command to its executable surface: drop heredoc payloads and
    plain quoted string literals. Kept as-is when a shell evaluator could
    execute them, or when a quoted segment contains $/backtick expansion."""
    if not INTERPRETER_HEREDOC_RE.search(cmd):
        cmd = HEREDOC_RE.sub("<<HEREDOC_DATA", cmd)
    if not SHELL_EVAL_RE.search(cmd):
        cmd = QUOTED_RE.sub(
            lambda m: m.group(0) if ("$" in m.group(0) or "`" in m.group(0)) else "'DATA'",
            cmd)
    return cmd


def _target_risk(token):
    t = token.strip("'\"")
    if t in ("/", "/*", "~", "~/", "$HOME", "${HOME}", "$HOME/", "${HOME}/"):
        return 3
    if t.startswith(SAFE_TMP_PREFIXES):
        return 1
    if t.startswith(("/", "~", "$")) or ".." in t or t in (".", "./", "*"):
        return 2
    return 1


def classify_rm(surface):
    """0 = no recursive-force rm; 1 = safe targets; 2 = confirm; 3 = catastrophic."""
    worst = 0
    for m in RM_CMD_RE.finditer(surface):
        flag_tokens = m.group(1).split()
        short = "".join(t[1:] for t in flag_tokens
                        if t.startswith("-") and not t.startswith("--"))
        recursive = "r" in short.lower() or "--recursive" in flag_tokens
        force = "f" in short.lower() or "--force" in flag_tokens
        if not (recursive and force):
            continue
        targets = [t for t in m.group(2).split() if not t.startswith("-")]
        if not targets:
            worst = max(worst, 2)
            continue
        for t in targets:
            worst = max(worst, _target_risk(t))
    return worst


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


def emit_decision(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def deny(reason):
    emit_decision("deny", reason)


def ask(reason):
    emit_decision("ask", reason)


def log_decision(session_id, tool_name, channel, reason):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(TELEMETRY_PATH, "a") as f:
            f.write(json.dumps({
                "ts": round(time.time(), 3),
                "session_id": session_id,
                "hook": "pretool",
                "source": "pretool_gate",
                "channel": channel,
                "tool": tool_name,
                "reason": reason[:200],
            }) + "\n")
    except OSError:
        pass


def log_deny(session_id, tool_name, reason):
    log_decision(session_id, tool_name, "deny", reason)


def main():
    if os.environ.get("AIDE_JUDGE_BYPASS"):  # optimizer CLI child — no recursion
        sys.exit(0)
    if intervention_scope() != "full":
        sys.exit(0)  # prompt-only scope (default): no tool-call interventions
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
        if mode in AUTO_MODES:
            surface = scan_surface(tin.get("command", ""))
            rm_risk = classify_rm(surface)
            if rm_risk >= 3:
                reason = "Recursive delete aimed at root or home. Denied under auto-accept."
                log_decision(session_id, name, "deny", reason)
                deny(reason)
            if rm_risk == 2:
                reason = ("Recursive delete outside the workspace (absolute, parent, or "
                          "variable path). Confirm before it runs.")
                log_decision(session_id, name, "ask", reason)
                ask(reason)
            if ASK_DESTRUCTIVE_RE.search(surface):
                reason = "Destructive command under auto-accept. Confirm before it runs."
                log_decision(session_id, name, "ask", reason)
                ask(reason)

    sys.exit(0)


if __name__ == "__main__":
    main()

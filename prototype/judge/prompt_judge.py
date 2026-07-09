#!/usr/bin/env python3
"""Real-time Prompt Judge — UserPromptSubmit hook for Claude Code.

Tier-1 deterministic rules + mined-pattern rulebook matching.
Stdlib only. Happy-path target: <150ms. No LLM calls in this file —
tier-2 escalation is deliberately out of the hot path (see README).

Channels:
  stdout notice -> visible nudge to the user (via systemMessage)
  inject        -> silent additionalContext steer to the agent
  transform     -> prompt passes through; agent is directed to act on an
                   optimized rewrite; user sees one "prompt optimised" line
  block         -> prompt erased, reason shown (rulebook patterns only)

Design doc: the AIDE design notes
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from optimizer import (TRANSFORM_PRIORITY, optimize, transform_context,
                       transform_notice)
from session_state import turn_reset

DATA_DIR = Path(os.environ.get("CLAUDE_JUDGE_HOME", Path.home() / ".claude-judge"))
RULEBOOK_PATH = DATA_DIR / "rulebook.json"
TELEMETRY_PATH = DATA_DIR / "telemetry.jsonl"
TELEMETRY_MAX_BYTES = 5 * 1024 * 1024
MARKS_PATH = DATA_DIR / "session_marks.json"
PENDING_TRANSFORM_PATH = DATA_DIR / "pending_transform.md"
COMPACT_MEMORY_DIR = DATA_DIR / "compact-memory"
STATE_DIR = DATA_DIR / "session_state"

# Thresholds — guesses to be calibrated against mined history.
BOUNDARY_CONTEXT_PCT = 0.4   # nudge if carrying >=40% of window across a task boundary
DEFAULT_WINDOW = 200_000     # fallback when model window can't be detected
SIMILARITY_BLOCK = 0.85
TASK_BOUNDARY_MAX_SIM = 0.2  # below this overlap with recent turns = new task
TAIL_BYTES = 512_000  # read at most this much of the transcript tail

SWITCH_MARKER_RE = re.compile(
    r"\b(new task|next task|next up|different (thing|topic)|switching to"
    r"|moving on|let'?s (now )?(move|switch) (on )?to|unrelated)\b", re.I)
SHIP_RE = re.compile(r"\b(commit|push|deploy|merge (this|it|the)|release|ship (it|this))\b", re.I)

CORRECTION_RE = re.compile(
    r"\b(no[, ]+i meant|that'?s (not|wrong)|not what i (asked|meant|wanted)"
    r"|you (broke|missed|ignored)|try again|still (wrong|broken|failing|not working)"
    r"|undo that|revert that|wrong file|didn'?t ask)\b", re.I)
VAGUE_VERBS = {"fix", "improve", "make", "update", "change", "refactor",
               "clean", "optimize", "help", "do"}
FILE_REF_RE = re.compile(r"[\w./\\-]+\.(py|js|ts|tsx|jsx|go|rs|java|rb|md|json|yaml|yml|toml|css|html|sql|sh)\b|/[\w./-]{3,}", re.I)
TEST_EVIDENCE_RE = re.compile(
    r"\b(pytest|npm test|npm run test|go test|cargo test|jest|vitest|unittest"
    r"|mvn test|rspec|tox|make test)\b", re.I)
ASK_SPLIT_RE = re.compile(r"\b(?:and then|after that|then also|also|additionally|plus)\b|;|\n\s*[-*]|\n\s*\d+[.)]", re.I)
CONTINUATION_SUMMARY_RE = re.compile(
    r"^\s*this session is being continued from a previous conversation",
    re.I,
)
HANDOFF_RE = re.compile(
    r"\b(next\s+session\s+(prompt|handoff|handover|brief)|"
    r"session\s+hand(-|\s)?(off|over)|"
    r"continuation\s+prompt|"
    r"prepare\s+for\s+next\s+session|"
    r"what'?s\s+done,?\s+what'?s\s+(left|pending|remaining)|"
    r"update\s+all\s+docs.{0,40}next\s+session)\b",
    re.I,
)
POLL_CMD_RE = re.compile(r"\b(sleep|tail\s+-f|watch)\b", re.I)

WEB_TOOLS = {"WebFetch", "WebSearch"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
STACK_TRACE_RE = re.compile(
    r"(Traceback \(most recent call last\)|^\s+at [\w.$<>]+ ?\(.+:\d+|"
    r"\b[A-Z][a-zA-Z]*(Error|Exception)\b\s*:|panicked at|"
    r"^\s*File \".+\", line \d+|Segmentation fault|core dumped)",
    re.M)
REPRO_MARKER_RE = re.compile(
    r"\b(reproduce|repro steps?|steps to|expected|actual(ly)? (behav|output|result)"
    r"|when i (click|run|open|call|submit)|after (running|clicking|calling)"
    r"|happens when|triggered by)\b", re.I)
RESEARCH_INTENT_RE = re.compile(
    r"\b(research|investigate|look up|search (the )?web|find (docs|documentation"
    r"|articles|examples online)|latest (news|version|release)|compare (libraries"
    r"|frameworks|tools)|read the docs)\b", re.I)
INTERNAL_TOOL_RE = re.compile(
    r"\bmcp__\w+|\b(WebFetch|WebSearch|MultiEdit|NotebookEdit)\b", re.I)
PLAN_MARKER_RE = re.compile(
    r"/(plan|architect|ask|think|spec|goal)\b|"
    r"\b(plan|approach|strategy|steps|breakdown|before\s+(coding|editing)|"
    r"do\s+not\s+edit\s+yet)\b",
    re.I)

RULE_PRIORITY = {
    "R2": 0, "R9": 1,
    "R1": 10, "R10": 10, "R11": 10, "R12": 10, "R14": 10, "R17": 10,
    "R18": 8, "R19": 9, "R20": 10, "R22": 12,
    "R3": 20, "R5": 30, "R13": 30, "R15": 30, "R21": 25,
    "R4": 40, "R6": 40, "R7": 35, "R16": 40,
}
# Merge order when more injects fire than the budget allows.
INJECT_MERGE_PRIORITY = ["R18", "R19", "R20", "R7", "R21", "R3", "R6", "R13", "R16"]
MAX_NOTICES_PER_PROMPT = 1
MAX_NOTICES_PER_SESSION = 3
MAX_BLOCKS_PER_SESSION = 1
COOLDOWN_PROMPTS = 10
MAX_INJECTIONS_PER_PROMPT = 2

RULE_TO_PATTERN = {
    "R18": "webfetch_chain",
    "R19": "repo_context_dumping",
    "R20": "context_stuffing_single_turn",
    "R21": "error_without_repro",
    "R22": "edit_before_plan",
    "R2": "retry_same_prompt",
    "R7": "missing_verification",
}

DEFAULT_ESCALATION = {
    "webfetch_chain": ["inject", "inject", "stdout", "pretool"],
    "repo_context_dumping": ["inject", "inject", "stdout"],
    "context_stuffing_single_turn": ["inject", "inject", "stdout"],
    "error_without_repro": ["transform", "inject", "stdout"],
    "edit_before_plan": ["stdout", "stdout", "inject"],
    "retry_same_prompt": ["transform", "transform", "stdout"],
    "missing_verification": ["inject", "inject", "stdout"],
}
# Legacy rulebooks may still carry "block" rungs for retry_same_prompt;
# blocks on the prompt path are replaced by transforms (10-star UX).
LEGACY_RUNG_MAP = {"block": "transform"}


def rung_for(rule_id, marks, rulebook):
    """Current escalation channel for a rule (index into per-pattern ladder)."""
    pattern = RULE_TO_PATTERN.get(rule_id)
    if not pattern:
        return "inject"
    ladder = (rulebook.get("escalation") or {}).get(pattern) or DEFAULT_ESCALATION.get(
        pattern, ["inject"])
    idx = (marks.get("_escalation") or {}).get(pattern, 0)
    return ladder[min(idx, len(ladder) - 1)]


def record_escalation_receipt(rule_id, marks, channel):
    """Store last-fired rung for post-session / re-mine telemetry read-back."""
    pattern = RULE_TO_PATTERN.get(rule_id)
    if not pattern:
        return
    esc = marks.setdefault("_escalation", {})
    esc[f"_last_{pattern}"] = {"rule": rule_id, "channel": channel, "rung": esc.get(pattern, 0)}


# ---------------------------------------------------------------- utilities

def word_set(text):
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def similarity(a, b):
    """Jaccard similarity over word sets."""
    sa, sb = word_set(a), word_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def read_tail_entries(transcript_path):
    """Parse the tail of the session JSONL into a list of dict entries."""
    try:
        p = Path(transcript_path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # drop partial line
            raw = f.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return []
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def is_real_user_prompt(entry):
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None  # machine-generated tool-result turn
        text = " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith(("<", "/")):
        return None
    return text


def entry_has_error(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        return any(isinstance(c, dict) and c.get("type") == "tool_result"
                   and c.get("is_error") for c in content)
    return False


def _parse_ts(value):
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _prefix_signature(text):
    return re.sub(r"\d+", "#", (text or "")[:30])


def _bash_commands_since_prompt(entries, prompt_entry_index):
    commands = []
    for entry in entries[prompt_entry_index + 1:]:
        if entry.get("type") == "user" and is_real_user_prompt(entry):
            break
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        items = content if isinstance(content, list) else [content] if isinstance(content, dict) else []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            if item.get("name") != "Bash":
                continue
            tool_input = item.get("input") or {}
            command = tool_input.get("command") if isinstance(tool_input, dict) else ""
            if command:
                commands.append(re.sub(r"\s+", " ", command).strip())
    return commands


def _longest_bash_run(commands):
    if not commands:
        return 0
    best = 1
    run = 1
    for prev, curr in zip(commands, commands[1:]):
        if curr and curr == prev and not POLL_CMD_RE.search(curr):
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def _tool_uses(entries, start=0, stop=None):
    """Yield (index, name, input_dict) for every assistant tool_use in range."""
    stop = len(entries) if stop is None else stop
    for i in range(start, stop):
        entry = entries[i]
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        items = content if isinstance(content, list) else []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                yield i, item.get("name") or "", (item.get("input") or {})


def _last_run_slice(user_prompts):
    """[start, end) indices for the agent work evaluated on this prompt (one-prompt-late)."""
    if len(user_prompts) >= 2:
        return user_prompts[-2][0], user_prompts[-1][0]
    if user_prompts:
        return user_prompts[-1][0], None
    return -1, None


def _longest_web_chain(entries, start, end=None):
    """Longest consecutive WebFetch/WebSearch run in entries[start+1:end)."""
    end = len(entries) if end is None else end
    best = run = 0
    for _, name, _inp in _tool_uses(entries, start + 1, end):
        if name in WEB_TOOLS:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _reads_before_first_edit(entries):
    """(#Read/Glob/Grep calls before first edit tool, top read paths). Session-tail scope."""
    reads = 0
    paths = {}
    for _, name, inp in _tool_uses(entries):
        if name in EDIT_TOOLS:
            return reads, _top_paths(paths)
        if name in ("Read", "Glob", "Grep"):
            reads += 1
            p = inp.get("file_path") or inp.get("path") or ""
            if isinstance(p, str) and "." in Path(p).name:
                paths[p] = paths.get(p, 0) + 1
    return reads, _top_paths(paths)


def _top_paths(paths, n=5):
    return [p for p, _ in sorted(paths.items(), key=lambda kv: -kv[1])[:n]]


def _cache_read_series(entries, n=6):
    series = []
    for e in entries:
        if e.get("type") != "assistant":
            continue
        u = (e.get("message") or {}).get("usage") or {}
        cr = u.get("cache_read_input_tokens")
        if isinstance(cr, int):
            series.append(cr)
    return series[-n:]


def _last_error_snippet(entries, start, max_len=300):
    """First line of the most recent tool_result error text since `start`."""
    for entry in reversed(entries[start + 1:]):
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error"):
                text = item.get("content")
                if isinstance(text, list):
                    text = " ".join(c.get("text", "") for c in text
                                    if isinstance(c, dict) and c.get("type") == "text")
                if isinstance(text, str) and text.strip():
                    return text.strip().splitlines()[0][:max_len]
    return ""


def _keyword_overlap(prompt, keywords):
    words = word_set(prompt)
    keys = {k.lower() for k in keywords}
    if not keys:
        return 0.0
    return len(words & keys) / len(keys)


def _session_wall_clock_hours(entries):
    timestamps = [_parse_ts(e.get("timestamp")) for e in entries if e.get("timestamp")]
    timestamps = [ts for ts in timestamps if ts]
    if len(timestamps) < 2:
        return 0.0
    return max(0.0, (timestamps[-1] - timestamps[0]).total_seconds() / 3600)


def _compaction_continuation_seen(entries, lookback=3):
    seen = 0
    for entry in reversed(entries):
        if entry.get("type") != "user":
            continue
        text = is_real_user_prompt(entry) or _content_text(entry)
        if text and CONTINUATION_SUMMARY_RE.search(text):
            return 1
        seen += 1
        if seen >= lookback:
            break
    return 0


def _content_text(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


# ---------------------------------------------------------------- signals

def detect_window(entries):
    """Model context window from the transcript's model field. 1M-aware."""
    for e in reversed(entries):
        if e.get("type") == "assistant":
            model = ((e.get("message") or {}).get("model") or "").lower()
            if model:
                if "1m" in model or "[1m]" in model:
                    return 1_000_000
                return 200_000
    return DEFAULT_WINDOW


def compute_signals(prompt, entries, cwd, rulebook=None):
    """Fixed signal vocabulary shared with the pattern schema."""
    rulebook = rulebook or {}
    context_tokens = 0
    last_cache_read = -1
    for e in reversed(entries):
        if e.get("type") == "assistant":
            u = (e.get("message") or {}).get("usage") or {}
            if not u:
                continue
            context_tokens = (u.get("input_tokens", 0)
                              + u.get("cache_creation_input_tokens", 0)
                              + u.get("cache_read_input_tokens", 0))
            last_cache_read = u.get("cache_read_input_tokens", 0)
            break

    user_prompts = []   # (index_in_entries, text)
    for i, e in enumerate(entries):
        text = is_real_user_prompt(e)
        if text:
            user_prompts.append((i, text))

    # Failure flag per prior prompt: requires ACTUAL tool errors before the
    # next prompt (corrections alone feed R3/miner, not R2's block — tightened
    # so a block always rests on objective evidence).
    failed_prompts = []
    for n, (idx, text) in enumerate(user_prompts):
        end = user_prompts[n + 1][0] if n + 1 < len(user_prompts) else len(entries)
        if any(entry_has_error(entries[j]) for j in range(idx + 1, end)):
            failed_prompts.append(text)

    # Consecutive error turns from the end
    consecutive_errors = 0
    for e in reversed(entries):
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            if entry_has_error(e):
                consecutive_errors += 1
            else:
                break

    recent = [t for _, t in user_prompts[-5:]]
    recent_corrections = sum(1 for t in recent if CORRECTION_RE.search(t))

    blob = json.dumps(entries[-100:]) if entries else ""
    has_test_evidence = 1 if TEST_EVIDENCE_RE.search(blob) else 0
    has_file_edits = 1 if re.search(r'"name":\s*"(Edit|Write|MultiEdit|NotebookEdit)"', blob) else 0

    # Idle time since last transcript entry (drives cache-cold detection)
    idle_seconds = 0
    for e in reversed(entries):
        ts = e.get("timestamp")
        if ts:
            try:
                from datetime import datetime, timezone
                last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                idle_seconds = max(0, int((datetime.now(timezone.utc) - last).total_seconds()))
            except (ValueError, TypeError):
                pass
            break

    # Task boundary: explicit switch marker, or low overlap with recent prompts
    window = detect_window(entries)
    # Low overlap is the primary evidence; an explicit switch marker only
    # relaxes the threshold. A marker alone never declares a boundary —
    # "now handle the edge case" is usually the SAME task.
    recent_texts = [t for _, t in user_prompts[-3:]]
    overlap = max((similarity(prompt, t) for t in recent_texts), default=0.0)
    threshold = 0.35 if SWITCH_MARKER_RE.search(prompt) else TASK_BOUNDARY_MAX_SIM
    task_boundary = 1 if (recent_texts and overlap < threshold) else 0

    words = re.findall(r"\S+", prompt)
    asks = [s for s in ASK_SPLIT_RE.split(prompt) if s and len(s.split()) > 3]

    claude_md = 1 if (Path(cwd) / "CLAUDE.md").exists() else 0
    is_git = (Path(cwd) / ".git").exists()

    sim_failed = max((similarity(prompt, f) for f in failed_prompts), default=0.0)
    sim_recent = max((similarity(prompt, t) for t in recent[:-1] or recent), default=0.0)

    last_prompt_index = user_prompts[-1][0] if user_prompts else -1
    run_start, run_end = _last_run_slice(user_prompts)
    run_end = len(entries) if run_end is None else run_end
    last_run_commands = _bash_commands_since_prompt(entries, run_start)
    # Cap bash commands to the previous agent run slice when bounded.
    if run_end < len(entries):
        capped = []
        for entry in entries[run_start + 1:run_end]:
            if entry.get("type") == "user" and is_real_user_prompt(entry):
                break
            if entry.get("type") != "assistant":
                continue
            content = (entry.get("message") or {}).get("content")
            items = content if isinstance(content, list) else []
            for item in items:
                if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Bash":
                    cmd = (item.get("input") or {}).get("command", "")
                    if cmd:
                        capped.append(re.sub(r"\s+", " ", cmd).strip())
        if capped:
            last_run_commands = capped
    bash_run = _longest_bash_run(last_run_commands)
    repeated_command = ""
    if bash_run >= 2:
        for prev, curr in zip(last_run_commands, last_run_commands[1:]):
            if curr == prev and not POLL_CMD_RE.search(curr):
                repeated_command = curr
    errors_since_prompt = 0
    if last_prompt_index >= 0:
        for entry in entries[last_prompt_index + 1:]:
            if entry.get("type") == "user" and is_real_user_prompt(entry):
                break
            if entry_has_error(entry):
                errors_since_prompt += 1

    web_chain = _longest_web_chain(entries, run_start, run_end)
    reads_before_edit, hotspot_paths = _reads_before_first_edit(entries)
    cache_series = _cache_read_series(entries)
    cache_jump = 0.0
    if len(cache_series) >= 2 and cache_series[-2] >= 20_000:
        cache_jump = round(cache_series[-1] / max(1, cache_series[-2]), 2)
    peak_cache_read = max(cache_series, default=0)
    last_error_snippet = _last_error_snippet(entries, last_prompt_index)

    prefix = _prefix_signature(prompt)
    template_match = 0
    for template in rulebook.get("template_signatures", []):
        if template.get("signature") == prefix:
            template_match = 1
            break

    cluster_match = 0
    for cluster in rulebook.get("correction_clusters", []):
        if _keyword_overlap(prompt, cluster.get("keywords", [])) >= 0.5:
            cluster_match = 1
            break

    active_model = ""
    for entry in reversed(entries):
        if entry.get("type") == "assistant":
            active_model = ((entry.get("message") or {}).get("model") or "")
            if active_model:
                break

    return {
        "context_tokens": context_tokens,
        "context_window": window,
        "context_pct": round(context_tokens / window, 3) if window else 0.0,
        "task_boundary": task_boundary,
        "last_cache_read": last_cache_read,
        "idle_seconds": idle_seconds,
        "hours_since_last_event": round(idle_seconds / 3600, 3),
        "session_wall_clock_hours": round(_session_wall_clock_hours(entries), 3),
        "compaction_continuation_seen": _compaction_continuation_seen(entries),
        "session_has_file_edits": has_file_edits,
        "turns": len(user_prompts),
        "consecutive_error_turns": consecutive_errors,
        "last_run_error_count": errors_since_prompt,
        "last_run_retry_spiral": bash_run,
        "recent_corrections": recent_corrections,
        "prompt_word_count": len(words),
        "prompt_question_marks": prompt.count("?"),
        "prompt_has_file_ref": 1 if FILE_REF_RE.search(prompt) else 0,
        "prompt_imperative_asks": max(1, len(asks)),
        "prompt_similarity_to_failed": round(sim_failed, 3),
        "prompt_similarity_to_recent": round(sim_recent, 3),
        "prompt_matches_template": template_match,
        "prompt_matches_handoff": 1 if HANDOFF_RE.search(prompt) else 0,
        "prompt_matches_correction_cluster": cluster_match,
        "prompt_prefix": prefix,
        "session_has_test_evidence": has_test_evidence,
        "is_first_prompt": 1 if len(user_prompts) == 0 else 0,
        "claude_md_present": claude_md,
        "is_git_repo": 1 if is_git else 0,
        "active_model": active_model,
        # v1 artifact signals
        "last_run_web_chain": web_chain,
        "last_run_repeated_command": repeated_command,
        "last_run_error_snippet": last_error_snippet,
        "reads_before_first_edit": reads_before_edit,
        "hotspot_paths": hotspot_paths,
        "cache_read_jump": cache_jump,
        "peak_cache_read": peak_cache_read,
        "prompt_has_stack_trace": 1 if STACK_TRACE_RE.search(prompt) else 0,
        "prompt_has_repro_markers": 1 if REPRO_MARKER_RE.search(prompt) else 0,
        "prompt_requests_research": 1 if RESEARCH_INTENT_RE.search(prompt) else 0,
        "prompt_has_plan_marker": 1 if PLAN_MARKER_RE.search(prompt) else 0,
        "prompt_mentions_internal_tool_names": 1 if INTERNAL_TOOL_RE.search(prompt) else 0,
        "prompt_similarity_to_tail": round(
            max((similarity(prompt, t) for t in recent), default=0.0), 3),
    }


# ---------------------------------------------------------------- artifacts

def build_error_steer(sig):
    """Structured steer for an error dump without repro context (silent inject)."""
    return (
        "The user pasted an error without reproduction context. Treat it as:\n"
        "<error_report>\n"
        "  <error>the pasted trace</error>\n"
        "  <trigger>unknown — infer from code, or ask one targeted question</trigger>\n"
        "  <expected>unknown</expected>\n"
        "  <runtime>unknown</runtime>\n"
        "</error_report>\n"
        "Identify the root cause before editing. Consider null/undefined defaults, "
        "async state, missing data, and race conditions. Propose the smallest fix "
        "plus one verification step.")


def build_verification_packet(rulebook):
    """<verification> inject; commands come from the per-project rulebook when known."""
    commands = rulebook.get("verify_commands") or []
    if commands:
        cmd_lines = "\n".join(f"    {c}" for c in commands[:4])
        confidence = "high"
    else:
        cmd_lines = "    [run the project's test/build commands]"
        confidence = "low"
    return (
        f"<verification confidence=\"{confidence}\">\n"
        f"  <commands>\n{cmd_lines}\n  </commands>\n"
        "  <evidence_required>State pass/fail output before claiming done.</evidence_required>\n"
        "  <fallback>If tests are unavailable or flaky, say why and run the smallest "
        "relevant check.</fallback>\n"
        "</verification>")


def save_pending_transform(text):
    """Persist a suggested rewrite so the /aide command can resubmit it cheaply."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_TRANSFORM_PATH.write_text(
            f"<!-- expires:{int(time.time()) + 600} -->\n{text}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- rules

def fire_record(rule, rule_class, emits):
    """Build a fired record. emits: list of (channel, message) tuples."""
    return {"rule": rule, "class": rule_class, "emits": list(emits)}


def _emit_channel(record, channel):
    for ch, _msg in record.get("emits", []):
        if ch == channel:
            return True
    return False


def run_tier1(prompt, sig, marks, thresholds=None, rulebook=None):
    """Returns (block_reason|None, block_rule|None, fired[])."""
    thresholds = thresholds or {}
    rulebook = rulebook or {}
    stale_hours = thresholds.get("stale_hours", 12)
    stale_tokens = thresholds.get("stale_carried_tokens", 20_000)
    marathon_hours = thresholds.get("marathon_hours", 24)
    web_chain_min = thresholds.get("web_chain_min", 12)
    reads_dump_min = thresholds.get("reads_before_edit_min", 25)
    stuffing_peak = thresholds.get("stuffing_peak_cache", 150_000)
    block, block_rule, fired = None, None, []

    followup = (sig["prompt_word_count"] < 20
                or sig["prompt_similarity_to_tail"] >= 0.15)

    # R1 — context carried across a task boundary. Never blocks: mid-task
    # large context is legitimate; interrupting deep work costs more than tokens.
    if (sig["task_boundary"] and sig["context_pct"] >= BOUNDARY_CONTEXT_PCT
            and sig["turns"] >= 2):
        fired.append(fire_record("R1", "B", [(
            "stdout",
            f"[judge] This looks like a new task, but you're carrying "
            f"~{sig['context_tokens'] // 1000}k tokens ({sig['context_pct']:.0%} of window) "
            f"of previous-task history into it. Split kit: finish or park the current task, "
            f"then run /clear and open the new task with files + done-condition. "
            f"Ignore this if it's actually the same task.",
        )]))

    if block is None and sig["prompt_similarity_to_failed"] >= SIMILARITY_BLOCK and sig["turns"] > 0:
        r2 = marks.setdefault("R2", {})
        rung = rung_for("R2", marks, rulebook)
        rung = LEGACY_RUNG_MAP.get(rung, rung)
        if r2.get("override_count", 0) >= 2 or rung in ("stdout", "notice"):
            fired.append(fire_record("R2", "B", [(
                "stdout",
                "[judge] This prompt matches one that already failed. "
                "Add what changed, or expect the same failure.",
            )]))
        else:
            fired.append(fire_record("R2", "B", [
                ("transform", ""),
                ("stdout",
                 "[judge] This prompt matches one that just failed — say what "
                 "changed, or expect the same failure."),
            ]))
        record_escalation_receipt("R2", marks, rung)

    if sig["consecutive_error_turns"] >= 3 or sig["recent_corrections"] >= 2:
        fired.append(fire_record("R3", "B", [
            ("stdout",
             "[judge] Several consecutive failures/corrections. Consider stepping back: "
             "restate the goal and constraints instead of patching the last attempt."),
            ("inject",
             "The last several turns show repeated errors or user corrections. "
             "Before acting, briefly restate your understanding of the goal and "
             "propose a different approach than the failing one."),
        ]))

    if sig["is_git_repo"] and not sig["claude_md_present"] and not marks.get("r4"):
        marks["r4"] = True
        fired.append(fire_record("R4", "B", [(
            "stdout",
            "[judge] No CLAUDE.md in this repo. Run /init once — every future "
            "session starts smarter.",
        )]))

    if (sig["is_first_prompt"] and sig["prompt_word_count"] < 12
            and not sig["prompt_has_file_ref"]
            and (word_set(prompt) & VAGUE_VERBS)):
        fired.append(fire_record("R5", "A", [
            ("transform", ""),
            ("stdout",
             "[judge] Short, unanchored opener. Naming files, the expected behavior, "
             "and how to verify success usually saves a round trip."),
        ]))

    if sig["prompt_imperative_asks"] >= 4:
        fired.append(fire_record("R6", "A", [
            ("transform", ""),
            ("inject",
             "This prompt bundles several distinct tasks. Propose a short plan, "
             "confirm ordering with the user if priorities are unclear, and "
             "complete tasks one at a time with verification between steps."),
        ]))

    if (not sig["session_has_test_evidence"] and sig["turns"] >= 3
            and sig["prompt_word_count"] >= 8
            and re.search(r"\b(implement|add|build|create|write|fix)\b", prompt, re.I)):
        rung = rung_for("R7", marks, rulebook)
        packet = (
            "No tests or builds have been run this session. Before reporting done:\n"
            + build_verification_packet(rulebook))
        if rung in ("stdout", "notice"):
            fired.append(fire_record("R7", "B", [(
                "stdout",
                "[judge] No verification yet — run tests before claiming done.",
            )]))
        else:
            fired.append(fire_record("R7", "B", [("inject", packet)]))
        record_escalation_receipt("R7", marks, rung)

    if (SHIP_RE.search(prompt) and sig["session_has_file_edits"]
            and not sig["session_has_test_evidence"]):
        fired.append(fire_record("R9", "B", [(
            "stdout",
            "[judge] You're about to ship changes this session, but no tests or "
            "builds have run. One verification pass now is cheaper than a revert later.",
        )]))

    if (sig["idle_seconds"] > 300 and sig["context_tokens"] > 50_000
            and sig["turns"] >= 2):
        fired.append(fire_record("R10", "B", [(
            "stdout",
            f"[judge] You've been idle ~{sig['idle_seconds'] // 60} min — the prompt "
            f"cache has expired, so this turn re-reads ~{sig['context_tokens'] // 1000}k "
            f"tokens at full price. If you're between tasks, /clear first.",
        )]))

    if (sig["hours_since_last_event"] > stale_hours
            and sig["context_tokens"] > stale_tokens
            and sig["prompt_word_count"] < 100
            and not sig["compaction_continuation_seen"]
            and not marks.get("r11_delivered")):
        marks["r11_delivered"] = True
        msg = (f"[judge] Resuming after ~{sig['hours_since_last_event']:.0f}h idle "
               f"with ~{sig['context_tokens'] // 1000}k carried tokens")
        if sig["recent_corrections"]:
            msg += f" and {sig['recent_corrections']} recent correction(s)"
        if sig["prompt_similarity_to_recent"] < 0.2:
            msg += " — this looks like a NEW task in an old shell"
        msg += ". /clear or /compact first unless you need the history."
        fired.append(fire_record("R11", "B", [("stdout", msg)]))
        record_escalation_receipt("R11", marks, "stdout")

    if ((sig["session_wall_clock_hours"] > marathon_hours or sig["turns"] > 60)
            and not marks.get("r12")):
        marks["r12"] = True
        fired.append(fire_record("R12", "B", [(
            "stdout",
            "[judge] This session has crossed a day/N-task boundary. "
            "Cut a handoff brief and start fresh unless you're mid-task.",
        )]))

    if sig["prompt_question_marks"] >= 3 and sig["prompt_word_count"] < 200:
        fired.append(fire_record("R13", "A", [
            ("transform", ""),
            ("inject",
             "The user asked multiple questions. Answer as a numbered "
             "checklist so nothing is skipped."),
        ]))

    if sig["last_run_retry_spiral"] >= 3:
        cmd = sig.get("last_run_repeated_command") or "the same command"
        err = sig.get("last_run_error_snippet")
        notice = (f"[judge] `{cmd[:60]}` ran {sig['last_run_retry_spiral']}x since your "
                  "last prompt. Put the missing environment context into CLAUDE.md "
                  "so the agent stops probing.")
        packet = (f"<last_command_failure>\n  command: {cmd[:120]}\n"
                  f"  repeated: {sig['last_run_retry_spiral']}x\n")
        if err:
            packet += f"  stderr: {err}\n"
        packet += ("</last_command_failure>\n"
                   "Do not rerun the same command. Diagnose environment, working "
                   "directory, dependencies, and script configuration first.")
        fired.append(fire_record("R17", "B", [("stdout", notice), ("inject", packet)]))

    if (sig["last_run_web_chain"] >= web_chain_min and followup
            and not sig["prompt_requests_research"]
            and not _rule_on_cooldown("R18", marks, sig["turns"])):
        rung = rung_for("R18", marks, rulebook)
        packet = (
            f"<source_policy expires=\"after_this_turn\">\n"
            f"The previous run fetched {sig['last_run_web_chain']} web pages in a row. "
            f"Summarize what was already fetched and prefer repo/local evidence next. "
            f"Use the web again only if specific external docs are required.\n"
            f"</source_policy>")
        if rung in ("stdout", "notice"):
            fired.append(fire_record("R18", "B", [(
                "stdout",
                f"[judge] Previous run fetched {sig['last_run_web_chain']} pages — "
                "summarize before fetching more.",
            )]))
        else:
            fired.append(fire_record("R18", "B", [("inject", packet)]))
        record_escalation_receipt("R18", marks, rung)
        _record_rule_fire("R18", marks, sig["turns"])

    if (sig["reads_before_first_edit"] >= reads_dump_min and followup
            and not sig["prompt_has_file_ref"]
            and not _rule_on_cooldown("R19", marks, sig["turns"])):
        rung = rung_for("R19", marks, rulebook)
        hotspots = sig.get("hotspot_paths") or []
        listing = "\n".join(f"  {p}" for p in hotspots) if hotspots else "  (no clear hotspots)"
        packet = (
            f"<known_relevant_files expires=\"after_this_turn\">\n{listing}\n"
            f"</known_relevant_files>\n"
            f"<repo_first_guidance>\n"
            f"{sig['reads_before_first_edit']} files/searches were read before the first edit. "
            f"Start from the files above; do not rescan the repository unless they prove "
            f"insufficient.\n</repo_first_guidance>")
        if rung in ("stdout", "notice"):
            fired.append(fire_record("R19", "B", [(
                "stdout",
                "[judge] Many files were read before the first edit — "
                "start from known hotspots, don't rescan.",
            )]))
        else:
            fired.append(fire_record("R19", "B", [("inject", packet)]))
        record_escalation_receipt("R19", marks, rung)
        _record_rule_fire("R19", marks, sig["turns"])

    if ((sig["cache_read_jump"] >= 2.0 or sig["peak_cache_read"] > stuffing_peak)
            and followup and not sig["compaction_continuation_seen"]
            and not _rule_on_cooldown("R20", marks, sig["turns"])):
        rung = rung_for("R20", marks, rulebook)
        packet = (
            f"<session_focus expires=\"after_this_turn\">\n"
            f"Context is heavy (peak cache read ~{sig['peak_cache_read'] // 1000}k tokens"
            + (f", last-turn jump {sig['cache_read_jump']:.1f}x" if sig["cache_read_jump"] else "")
            + "). Anchor on the user's current goal and the most recently edited files; "
            "ignore earlier exploratory material unless directly referenced.\n"
            "</session_focus>")
        if rung in ("stdout", "notice"):
            fired.append(fire_record("R20", "B", [(
                "stdout",
                "[judge] Context is heavy — anchor on the current goal.",
            )]))
        else:
            fired.append(fire_record("R20", "B", [("inject", packet)]))
        record_escalation_receipt("R20", marks, rung)
        _record_rule_fire("R20", marks, sig["turns"])

    if (sig["prompt_has_stack_trace"] and not sig["prompt_has_repro_markers"]
            and sig["prompt_word_count"] < 400):
        rung = rung_for("R21", marks, rulebook)
        packet = build_error_steer(sig)
        if rung == "transform":
            fired.append(fire_record("R21", "A", [
                ("transform", ""), ("inject", packet)]))
        elif rung in ("stdout", "notice"):
            fired.append(fire_record("R21", "A", [(
                "stdout",
                "[judge] Error pasted without repro steps — diagnose before editing.",
            )]))
        else:
            fired.append(fire_record("R21", "A", [("inject", packet)]))
        record_escalation_receipt("R21", marks, rung)

    plan_skip = (rulebook.get("plan_skip") or {}).get("active")
    if (plan_skip and sig["prompt_imperative_asks"] >= 3
            and sig["prompt_word_count"] >= 30
            and not sig["prompt_has_plan_marker"]
            and not marks.get("r22")):
        rung = rung_for("R22", marks, rulebook)
        marks["r22"] = True
        first_line = prompt.strip().splitlines()[0][:80]
        alt = (f"/plan {first_line}... — output: file-level plan, risks, "
               "out-of-scope, verification commands, stop point before edits.")
        if rung == "inject":
            fired.append(fire_record("R22", "C", [("inject", f"<plan_redirect>{alt}</plan_redirect>")]))
        else:
            fired.append(fire_record("R22", "C", [(
                "stdout",
                "[judge] Multi-part change and your history shows plan-skipping costs "
                f"rework. One-click alternative — copy:\n{alt}",
            )]))
        record_escalation_receipt("R22", marks, rung)

    return block, block_rule, fired


def evaluate_trigger(trigger, sig):
    ops = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
           "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
           "==": lambda a, b: a == b}
    conds = trigger.get("all", [])
    if not conds:
        return False
    for c in conds:
        val = sig.get(c.get("signal"))
        op = ops.get(c.get("op"))
        if val is None or op is None or not op(val, c.get("value")):
            return False
    return True


def _rule_on_cooldown(rule_id, marks, turn):
    record = marks.get(rule_id, {})
    if record.get("demoted"):
        return True
    last_turn = record.get("last_fired_turn", -999)
    if turn - last_turn < COOLDOWN_PROMPTS:
        return True
    return False


def _record_rule_fire(rule_id, marks, turn):
    record = marks.setdefault(rule_id, {})
    record["last_fired_turn"] = turn
    record["fire_count"] = record.get("fire_count", 0) + 1


def run_rulebook(sig, rulebook, marks, turn, prompt=""):
    """R8 + compiled class-C rules. Returns (block_reason|None, block_rule|None, fired[])."""
    block, block_rule, fired = None, None, []
    handoff = rulebook.get("handoff") or {}

    if (sig["prompt_matches_template"] and not marks.get("r14")
            and not _rule_on_cooldown("c_batch_template", marks, turn)):
        template = next((t for t in rulebook.get("template_signatures", [])
                         if t.get("signature") == sig.get("prompt_prefix")), None)
        sig_text = sig.get("prompt_prefix") or "this template"
        count = (template or {}).get("count", "?")
        headless = (
            f'claude -p --model sonnet -p "YOUR_TASK_HERE" '
            f'# template: {sig_text[:60]}')
        msg = (f"This prompt matches a repeated template ({count}×). "
               f"Run headless instead:\n  {headless}")
        fired.append(fire_record("R14", "C", [("stdout", f"[judge/pattern] {msg}")]))
        marks["r14"] = True
        _record_rule_fire("c_batch_template", marks, turn)

    if (sig["prompt_matches_handoff"] and handoff.get("active")
            and not marks.get("r15")
            and not _rule_on_cooldown("c_handoff_ritual", marks, turn)):
        fired.append(fire_record("R15", "C", [(
            "stdout", f"[judge/pattern] {handoff.get('message', '')}",
        )]))
        marks["r15"] = True
        _record_rule_fire("c_handoff_ritual", marks, turn)

    if (sig["prompt_matches_correction_cluster"]
            and not _rule_on_cooldown("c_correction_cluster", marks, turn)):
        cluster = next(
            (c for c in rulebook.get("correction_clusters", [])
             if _keyword_overlap(prompt, c.get("keywords", [])) >= 0.5),
            None,
        )
        hint = (cluster or {}).get("constraint_hint") or (
            "This correction has recurred across sessions.")
        fired.append(fire_record("R16", "C", [
            ("inject", hint),
            ("stdout",
             "[judge/pattern] Recurring correction — consider saving "
             "this constraint to CLAUDE.md."),
        ]))
        _record_rule_fire("c_correction_cluster", marks, turn)

    for pat in rulebook.get("patterns", []):
        if pat.get("user_status") in ("rejected", "muted"):
            continue
        rule_id = pat.get("id", pat.get("pattern", ""))
        if _rule_on_cooldown(rule_id, marks, turn):
            continue
        if not evaluate_trigger(pat.get("trigger", {}), sig):
            continue
        msg = (pat.get("action") or {}).get("message") or pat.get("description", "")
        channel = (pat.get("action") or {}).get("channel", "inject")
        rule_class = pat.get("class", "C")
        if pat.get("user_status") == "candidate" and channel == "stdout":
            channel = "inject"
        allowed_block = bool((pat.get("rights") or {}).get("blocking"))
        if channel == "block" and allowed_block and block is None:
            fired.append(fire_record(rule_id, rule_class, [("block", msg)]))
            block = msg
            block_rule = rule_id
        elif channel == "stdout":
            fired.append(fire_record(rule_id, rule_class, [("stdout", f"[judge/pattern] {msg}")]))
        else:
            fired.append(fire_record(rule_id, rule_class, [("inject", msg)]))
        _record_rule_fire(rule_id, marks, turn)
    return block, block_rule, fired


def _collect_emits(fired, channel):
    out = []
    for record in fired:
        rule = record["rule"]
        for ch, msg in record.get("emits", []):
            if ch == channel:
                out.append((rule, msg))
    return out


def merge_injections(fired):
    """Merge overflow injects into <carry_forward> instead of truncating."""
    injects = _collect_emits(fired, "inject")
    if len(injects) <= MAX_INJECTIONS_PER_PROMPT:
        return [msg for _rule, msg in injects]
    ordered = sorted(
        injects,
        key=lambda item: (INJECT_MERGE_PRIORITY.index(item[0])
                          if item[0] in INJECT_MERGE_PRIORITY else 99))
    keep = ordered[:MAX_INJECTIONS_PER_PROMPT]
    overflow = ordered[MAX_INJECTIONS_PER_PROMPT:]
    result = [msg for _rule, msg in keep]
    if overflow:
        extras = "\n\n".join(msg for _rule, msg in overflow)
        result.append(f"<carry_forward>\n{extras}\n</carry_forward>")
    return result


def apply_budget(fired, marks, turn):
    """Fatigue budget: max 1 notice, max 3/session, block cap, cooldowns, demotion."""
    session_notice_count = marks.get("_session_notices", 0)
    session_block_count = marks.get("_session_blocks", 0)
    suppressed = []
    block_reason = None
    block_rule = None
    for record in fired:
        rule = record["rule"]
        block_emits = [(ch, msg) for ch, msg in record.get("emits", []) if ch == "block"]
        if not block_emits:
            continue
        _ch, msg = block_emits[0]
        if session_block_count >= MAX_BLOCKS_PER_SESSION:
            record["emits"] = [e for e in record["emits"] if e[0] != "block"]
            record["emits"].append(("stdout", msg))
            suppressed.append(rule)
            continue
        block_reason = msg
        block_rule = rule
        marks["_session_blocks"] = session_block_count + 1
        break

    stdout_emits = _collect_emits(fired, "stdout")
    notices = []
    if stdout_emits:
        stdout_emits.sort(key=lambda item: RULE_PRIORITY.get(item[0], 50))
        notices = [stdout_emits[0][1]]
        for rule, _msg in stdout_emits[1:]:
            suppressed.append(rule)
    notices = notices[:MAX_NOTICES_PER_PROMPT]
    if session_notice_count >= MAX_NOTICES_PER_SESSION:
        for rule, _msg in stdout_emits:
            suppressed.append(rule)
        notices = []

    if notices:
        marks["_session_notices"] = session_notice_count + 1

    injections = merge_injections(fired)

    last_rule = marks.get("_last_rule")
    last_prompt_sim = marks.get("_last_prompt_sim", 0)
    if last_rule and last_prompt_sim > 0.8:
        marks.setdefault(last_rule, {})["demoted"] = True

    budgeted_fired = []
    for record in fired:
        rule = record["rule"]
        for ch, _msg in record.get("emits", []):
            budgeted_fired.append({
                "source": rule, "channel": ch, "rule_class": record["class"],
                "suppressed": rule in suppressed,
                "demoted": bool(marks.get(rule, {}).get("demoted")),
            })
    return block_reason, block_rule, notices, injections, budgeted_fired, suppressed


# ---------------------------------------------------------------- plumbing

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _rotate_telemetry_if_needed():
    try:
        if not TELEMETRY_PATH.exists() or TELEMETRY_PATH.stat().st_size <= TELEMETRY_MAX_BYTES:
            return
        archive_dir = DATA_DIR / "telemetry_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        TELEMETRY_PATH.rename(archive_dir / f"telemetry-{stamp}.jsonl")
    except OSError:
        pass


def append_telemetry(session_id, prompt, fired_records, extra=None):
    if not fired_records and not extra:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_telemetry_if_needed()
        ph = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        with open(TELEMETRY_PATH, "a") as f:
            for record in fired_records:
                src = record.get("source")
                f.write(json.dumps({
                    "ts": round(time.time(), 3), "session_id": session_id,
                    "hook": "user_prompt",
                    "source": src,
                    "pattern": RULE_TO_PATTERN.get(src),
                    "channel": record.get("channel"),
                    "rule_class": record.get("rule_class"),
                    "suppressed": record.get("suppressed", False),
                    "demoted": record.get("demoted", False),
                    "rung": (record.get("rung")),
                    "method": record.get("method"),
                    "latency_ms": record.get("latency_ms"),
                    "prompt_hash": ph, "response": "unknown",
                }) + "\n")
            if extra:
                f.write(json.dumps({
                    "ts": round(time.time(), 3), "session_id": session_id,
                    "hook": "user_prompt", "prompt_hash": ph, **extra,
                }) + "\n")
    except OSError:
        pass


def main():
    # Child sessions spawned by the optimizer's CLI polish set this guard;
    # exiting immediately prevents hook recursion.
    if os.environ.get("AIDE_JUDGE_BYPASS"):
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    prompt = (payload.get("prompt") or "").strip()
    session_id = payload.get("session_id", "unknown")
    cwd = payload.get("cwd") or os.getcwd()

    entries = read_tail_entries(payload.get("transcript_path"))
    rulebook = load_json(RULEBOOK_PATH, {})
    sig = compute_signals(prompt, entries, cwd, rulebook)
    turn = sig["turns"]

    # Always reset per-turn counters before any early exit (I3).
    turn_reset(session_id, turn, research_intent=bool(RESEARCH_INTENT_RE.search(prompt)))

    # '*' prefix bypasses rule evaluation for this prompt (power-user escape hatch).
    if prompt.startswith("*"):
        sys.exit(0)

    if not prompt or prompt.startswith(("/", "#")) or len(prompt.split()) <= 2:
        sys.exit(0)

    all_marks = load_json(MARKS_PATH, {})
    marks = all_marks.get(session_id, {})
    thresholds = rulebook.get("thresholds", {})
    ph = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    last_block = marks.get("_last_block")
    telemetry_extra = None
    if last_block and last_block.get("prompt_hash") == ph:
        block_rule = last_block.get("rule", "R2")
        r2 = marks.setdefault("R2", {})
        r2["override_count"] = r2.get("override_count", 0) + 1
        telemetry_extra = {
            "source": block_rule, "pattern": RULE_TO_PATTERN.get(block_rule),
            "channel": "block", "response": "override",
            "override_count": r2.get("override_count"),
        }
        marks.pop("_last_block", None)

    esc_meta = rulebook.get("escalation_meta") or {}
    esc_marks = marks.setdefault("_escalation", {})
    for pattern, meta in esc_meta.items():
        if isinstance(meta, dict) and pattern not in esc_marks:
            esc_marks[pattern] = meta.get("start_rung", 0)

    b1, br1, f1 = run_tier1(prompt, sig, marks, thresholds, rulebook)
    b2, br2, f2 = run_rulebook(sig, rulebook, marks, turn, prompt)
    fired = f1 + f2

    if sig.get("compaction_continuation_seen") and not marks.get("compact_ptr_delivered"):
        try:
            COMPACT_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            snapshots = sorted(COMPACT_MEMORY_DIR.glob("compact-*.md"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
            if snapshots:
                packet = (
                    f"<compact_recovery>\nFull session state snapshot: "
                    f"{snapshots[0]}\nRead it before continuing.\n</compact_recovery>")
                fired.append(fire_record("compact_recovery", "B", [("inject", packet)]))
                marks["compact_ptr_delivered"] = True
        except OSError:
            pass

    # Transform resolution: highest-priority rule that can build a rewrite
    # wins; its other emits are dropped. Losing rules keep their fallback
    # channels but lose the transform marker.
    transform = None
    transform_records = [r for r in fired if _emit_channel(r, "transform")]
    transform_records.sort(
        key=lambda r: (TRANSFORM_PRIORITY.index(r["rule"])
                       if r["rule"] in TRANSFORM_PRIORITY else 99))
    for record in transform_records:
        built = optimize(record["rule"], prompt, sig, rulebook)
        if built:
            transform = built
            record["emits"] = [("transform", built["text"])]
            break
    for record in transform_records:
        if transform is None or record["rule"] != transform["rule"]:
            record["emits"] = [e for e in record["emits"] if e[0] != "transform"]

    block, block_rule, notices, injections, fired_records, _suppressed = apply_budget(
        fired, marks, turn)

    for rec in fired_records:
        src = rec.get("source")
        if src in RULE_TO_PATTERN:
            rec["rung"] = (marks.get("_escalation") or {}).get(
                RULE_TO_PATTERN[src], 0)
        if (transform and rec.get("channel") == "transform"
                and src == transform["rule"]):
            rec["method"] = transform["method"]
            rec["latency_ms"] = transform["latency_ms"]

    marks["_last_rule"] = fired_records[0]["source"] if fired_records else None
    marks["_last_prompt_sim"] = sig["prompt_similarity_to_recent"]

    if block:
        marks["_last_block"] = {
            "prompt_hash": ph, "rule": block_rule or "R2", "ts": time.time(),
        }

    all_marks[session_id] = marks
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MARKS_PATH.write_text(json.dumps(dict(list(all_marks.items())[-50:])))
    except OSError:
        pass

    append_telemetry(session_id, prompt, fired_records, extra=telemetry_extra)

    if block:
        print(json.dumps({"decision": "block", "reason": block}))
        sys.exit(0)

    out = {}
    if transform:
        # 10-star UX: exactly one visible line; the rewrite rides along as
        # additionalContext and the agent acts on it.
        out["systemMessage"] = transform_notice(transform)
        save_pending_transform(transform["text"])
    elif notices:
        out["systemMessage"] = "\n".join(notices)
    ctx_parts = []
    if transform:
        ctx_parts.append(transform_context(transform))
    ctx_parts.extend(f"[prompt-judge] {m}" for m in injections)
    if ctx_parts:
        out["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(ctx_parts),
        }
    if out:
        print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    main()

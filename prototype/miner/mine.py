#!/usr/bin/env python3
"""History miner, stage 1: signals scan.

Walks ~/.claude/projects/**/*.jsonl, computes per-session stats, and flags
suspicious segments with cheap behavioral signals. No LLM calls here —
statistics find WHERE, label.py's LLM explains WHY.

Output: ~/.claude-judge/runs/YYYY-MM-DD_HHMMSS/  (timestamped archive, never overwritten)
        ~/.claude-judge/latest.json + latest → newest run
        ~/.claude-judge/rulebook.json         (published copy for live judge hooks)

Usage: python3 mine.py [--root ~/.claude/projects] [--max-sessions N] [--since DAYS] [--quiet]
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "judge"))
from prompt_judge import (CORRECTION_RE, similarity, is_real_user_prompt,
                          entry_has_error)
from judge_store import (
    ROOT,
    new_run_dir,
    finalize_run,
    get_latest_run_dir,
    list_runs,
    DETECTOR_VERSION,
)
from session_features import MalformedSessionEntry, VERIFY_RE, build_session_features
from generic_detectors import run_generic_detectors
from cross_session import aggregate, calibrate
from config_lint import render_config_report, run_config_lints
from rulebook_compiler import compile_rulebook
from web_export import build_dashboard_export, write_dashboard_export

DATA_DIR = ROOT
FINDINGS_REPORT = DATA_DIR / "findings_report.md"
FINDINGS_DEEP_DIVE = DATA_DIR / "findings_deep_dive.md"
CHANGE_FIRST = DATA_DIR / "change_first.md"
CONFIG_REPORT = DATA_DIR / "config_report.md"
DASHBOARD_DATA = DATA_DIR / "dashboard_data.json"
RULEBOOK_PATH = DATA_DIR / "rulebook.json"
TELEMETRY_PATH = DATA_DIR / "telemetry.jsonl"
RETRY_SIMILARITY = 0.8
RETRY_MIN_WORDS = 5          # short acks ("go ahead", "status?") are supervision, not retries
RETRY_LOOKBACK = 3           # compare against last N substantive prompts, not just n-1
CORRECTION_SCAN_CHARS = 80   # real corrections lead the message; ignore pasted content
CORRECTION_MAX_CHARS = 500   # real corrections are short; long messages are new instructions
EXCERPT_LEN = 300


class MinerTrace:
    """Live progress on stderr so the miner doesn't look hung."""

    def __init__(self, quiet=False):
        self.quiet = quiet
        self._t0 = time.perf_counter()
        self._stage_t = self._t0

    def stage(self, msg):
        if self.quiet:
            return
        now = time.perf_counter()
        if self._stage_t != self._t0:
            print(f"  done ({now - self._stage_t:.1f}s)", file=sys.stderr, flush=True)
        self._stage_t = now
        print(f"\n→ {msg}", file=sys.stderr, flush=True)

    def info(self, msg):
        if not self.quiet:
            print(f"  {msg}", file=sys.stderr, flush=True)

    def progress(self, current, total, label=""):
        if self.quiet or total <= 0:
            return
        pct = 100 * current / total
        short = (label or "")[-60:]
        line = f"  [{current:>{len(str(total))}}/{total}] {pct:4.0f}%  {short}"
        print(f"\r{line[:110]}", end="", file=sys.stderr, flush=True)

    def progress_end(self):
        if not self.quiet:
            print(file=sys.stderr, flush=True)

    def finish(self):
        if self.quiet:
            return
        now = time.perf_counter()
        if self._stage_t != self._t0:
            print(f"  done ({now - self._stage_t:.1f}s)", file=sys.stderr, flush=True)
        print(f"\n✓ Mine complete ({now - self._t0:.1f}s total)", file=sys.stderr, flush=True)


ACK_RE = re.compile(
    r"^\s*(go ahead|continue|proceed|yes|yep|ok(ay)?|sure|do it|next|status\??"
    r"|carry on|keep going|lgtm|approved?|sounds good|great|thanks?|done\??)\s*[.!?]*\s*$", re.I)


def is_well_formed_entry(entry):
    return (
        isinstance(entry, dict)
        and ("message" not in entry or isinstance(entry["message"], dict))
    )


def is_correction(text):
    """Real corrections are short and lead with the correction. Scanning whole
    long messages catches pasted content (including, memorably, this tool's
    own labeling prompt)."""
    return (len(text) <= CORRECTION_MAX_CHARS
            and bool(CORRECTION_RE.search(text[:CORRECTION_SCAN_CHARS])))


def parse_session(path):
    prompts = []          # (line_no, text)
    errors_after = {}     # prompt index -> error count before next prompt
    total_tokens = 0
    max_context = 0
    entries = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if is_well_formed_entry(entry):
                    entries.append(entry)
    except OSError:
        return None

    for e in entries:
        if e.get("type") == "assistant":
            u = (e.get("message") or {}).get("usage") or {}
            step = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0))
            total_tokens += step
            ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            max_context = max(max_context, ctx)

    for i, e in enumerate(entries):
        text = is_real_user_prompt(e)
        if text:
            prompts.append((i, text))

    streak_after = {}  # prompt index -> longest CONSECUTIVE error run before next prompt
    for n, (idx, _) in enumerate(prompts):
        end = prompts[n + 1][0] if n + 1 < len(prompts) else len(entries)
        count, run, best = 0, 0, 0
        for j in range(idx + 1, end):
            content = (entries[j].get("message") or {}).get("content")
            has_tool_result = (isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content))
            if not has_tool_result:
                continue
            if entry_has_error(entries[j]):
                count += 1
                run += 1
                best = max(best, run)
            else:
                run = 0
        errors_after[n] = count
        streak_after[n] = best

    return {"prompts": prompts, "errors_after": errors_after,
            "streak_after": streak_after,
            "total_tokens": total_tokens, "max_context": max_context,
            "n_entries": len(entries)}


def find_segments(session_id, s):
    """Flag suspicious moments: corrections, retry loops, error streaks.

    Unit-of-analysis discipline (AIDE): this miner looks for DEVELOPER
    patterns. Errors the agent recovered from without user intervention are
    agent/harness signals — they are counted separately, never flagged here.
    """
    segments = []
    prompts = s["prompts"]

    def substantive(text):
        return len(text.split()) >= RETRY_MIN_WORDS and not ACK_RE.match(text)

    for n, (_, text) in enumerate(prompts):
        # Correction after previous prompt (leading text only — see is_correction)
        if n > 0 and is_correction(text):
            segments.append({
                "session_id": session_id, "turn": n, "signal": "user_correction",
                "excerpt_prev": prompts[n - 1][1][:EXCERPT_LEN],
                "excerpt": text[:EXCERPT_LEN],
                "errors_after_prev": s["errors_after"].get(n - 1, 0),
            })
        # Retry loop: near-identical to a recent SUBSTANTIVE prompt (lookback
        # window skips interleaved corrections/acks). "go ahead" -> "go ahead"
        # is supervision of a long run, not a retry.
        if n > 0 and substantive(text):
            prior_substantive = [(m, t) for m, (_, t) in enumerate(prompts[:n])
                                 if substantive(t)][-RETRY_LOOKBACK:]
            match = next(((m, t) for m, t in prior_substantive
                          if similarity(text, t) >= RETRY_SIMILARITY), None)
            if match:
                segments.append({
                    "session_id": session_id, "turn": n, "signal": "retry_loop",
                    "excerpt_prev": match[1][:EXCERPT_LEN],
                    "excerpt": text[:EXCERPT_LEN],
                    "errors_after_prev": s["errors_after"].get(match[0], 0),
                })
        # Error streak: >=3 CONSECUTIVE errors AND the user had to intervene
        # (next prompt is a correction or a retry) or the session died there.
        if s["streak_after"].get(n, 0) >= 3:
            next_text = prompts[n + 1][1] if n + 1 < len(prompts) else None
            intervened = (next_text is not None
                          and (is_correction(next_text)
                               or (substantive(next_text) and substantive(text)
                                   and similarity(next_text, text) >= RETRY_SIMILARITY)))
            abandoned = next_text is None
            if intervened or abandoned:
                segments.append({
                    "session_id": session_id, "turn": n, "signal": "error_streak",
                    "excerpt": text[:EXCERPT_LEN],
                    "errors_after": s["errors_after"][n],
                    "consecutive": s["streak_after"][n],
                    "outcome": "abandoned" if abandoned else "user_intervened",
                })
    return segments


def collect_session_pair(path):
    """Single-pass feature + metadata collection."""
    try:
        features = build_session_features(path)
    except (OSError, json.JSONDecodeError, MalformedSessionEntry) as exc:
        print(f"Skipping {path}: {exc}", file=sys.stderr)
        return None
    metadata = session_metadata(path)
    return features, metadata


def enrich_findings(findings, features, metadata):
    enriched = []
    for finding in findings:
        row = dict(finding)
        row["session_id"] = features.session_id
        row["session_path"] = str(features.path)
        row["project"] = metadata["project"]
        row["git_branch"] = metadata["git_branch"]
        row["started_at"] = metadata["started_at"]
        row["ended_at"] = metadata["ended_at"]
        row["is_subagent_session"] = metadata["is_subagent_session"]
        enriched.append(row)
    return enriched


def compute_base_rates(session_pairs, all_findings):
    eligible = [f for f, m in session_pairs if not f.is_subagent]
    eligible_count = len(eligible) or 1
    rates = {}
    for pattern in {f["pattern"] for f in all_findings}:
        fires = sum(1 for f in all_findings if f["pattern"] == pattern)
        rates[pattern] = round(fires / eligible_count, 4)
    return rates


def count_by(items, key):
    counts = {}
    for item in items:
        value = item.get(key)
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def read_session_entries(path):
    entries = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if is_well_formed_entry(entry):
                    entries.append(entry)
    except OSError:
        return []
    return entries


def infer_project_from_path(path):
    path = Path(path)
    project_dir = path.parent.parent if path.parent.name == "subagents" else path.parent
    name = project_dir.name.lstrip("-")
    if not name:
        return str(project_dir)
    return "/" + name.replace("-", "/")


def session_metadata(path):
    entries = read_session_entries(path)
    cwd = next((entry.get("cwd") for entry in entries if entry.get("cwd")), "")
    git_branch = next((entry.get("gitBranch") for entry in entries if entry.get("gitBranch")), "")
    timestamps = [entry.get("timestamp") for entry in entries if entry.get("timestamp")]
    is_subagent = (
        Path(path).name.startswith("agent-")
        or Path(path).parent.name == "subagents"
        or any(bool(entry.get("isSidechain")) for entry in entries)
    )
    return {
        "project": cwd or infer_project_from_path(path),
        "git_branch": git_branch,
        "started_at": timestamps[0] if timestamps else "",
        "ended_at": timestamps[-1] if timestamps else "",
        "is_subagent_session": is_subagent,
    }


def user_prompt_timeline(entries):
    prompts = []
    for entry_index, entry in enumerate(entries):
        text = is_real_user_prompt(entry)
        if not text:
            continue
        prompts.append({
            "entry_index": entry_index,
            "turn": len(prompts),
            "timestamp": entry.get("timestamp", ""),
            "text": text,
        })
    return prompts


def _content_items(content):
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return content
    return []


def _tool_input_summary(tool_input):
    if not isinstance(tool_input, dict):
        return _trim(tool_input, 280)
    if tool_input.get("command"):
        return f"command={_trim(tool_input.get('command'), 260)}"
    for key in ("file_path", "path", "pattern"):
        if tool_input.get(key):
            return f"{key}={_trim(tool_input.get(key), 260)}"
    return _trim(json.dumps(tool_input, sort_keys=True, ensure_ascii=False), 280)


def tool_timeline(entries):
    tools = []
    for entry_index, entry in enumerate(entries):
        if entry.get("type") != "assistant":
            continue
        content = (entry.get("message") or {}).get("content")
        for item in _content_items(content):
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            tool_input = item.get("input", {})
            input_text = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
            name = item.get("name", "")
            tools.append({
                "entry_index": entry_index,
                "timestamp": entry.get("timestamp", ""),
                "name": name,
                "input_summary": _tool_input_summary(tool_input),
                "is_edit": name in {"Edit", "Write", "MultiEdit", "NotebookEdit"},
                "is_read": name in {"Read", "Grep", "Glob", "LS"},
                "is_verification": bool(VERIFY_RE.search(input_text)),
            })
    return tools


PATTERN_EXPLAINERS = {
    "missing_verification_criteria": {
        "title": "Edits landed without visible verification",
        "behavior": "The session made file edits, but the transcript did not show a test, build, lint, typecheck, screenshot, diff review, or other verification marker.",
        "impact": "This increases escaped-defect risk and makes it harder to know whether the final answer was evidence-backed or just plausible.",
        "better_next_time": "Ask for the exact verification command or artifact before accepting the task as done.",
    },
    "kitchen_sink_session": {
        "title": "Multiple workstreams accumulated in one large session",
        "behavior": "The session had high context usage and task-boundary language such as also/after that/another topic.",
        "impact": "The agent has to reason through stale or unrelated context, token cost rises, and review gets harder because separate decisions blur together.",
        "better_next_time": "Split unrelated work into a fresh or compacted session with one goal and one done condition.",
    },
    "edit_before_plan": {
        "title": "Edits started before a planning checkpoint",
        "behavior": "The task looked multi-step or ambiguous, but edits happened without a visible plan marker first.",
        "impact": "This raises the chance of rework, wrong-scope edits, or accepting implementation direction before tradeoffs are clear.",
        "better_next_time": "For multi-step work, require a short plan or ask-clarify checkpoint before edits begin.",
    },
    "repo_context_dumping": {
        "title": "Broad repo context before focused work",
        "behavior": "Large read/search output accumulated before edits.",
        "impact": "This spends context and attention before the agent has narrowed the file or symbol hypothesis.",
        "better_next_time": "Start with likely files or symbols, then expand reads only when the first hypothesis fails.",
    },
    "vague_underprompting": {
        "title": "Short vague prompt",
        "behavior": "A prompt asked the agent to proceed without target, expected behavior, or done criteria.",
        "impact": "The agent has to infer intent, which increases back-and-forth and wrong-scope work.",
        "better_next_time": "Name the target, expected behavior, and success signal.",
    },
    "typo_induced_ambiguity": {
        "title": "Typo or shorthand created ambiguity",
        "behavior": "A later correction indicated that wording or a target term had been mistyped or ambiguous.",
        "impact": "The agent may edit the wrong file/function or preserve a wrong assumption until corrected.",
        "better_next_time": "Confirm exact identifiers and scope before editing.",
    },
    "error_dump_without_repro_or_runtime_context": {
        "title": "Error shared without reproduction/runtime context",
        "behavior": "The prompt contained error evidence but did not include repro steps, expected/actual behavior, or runtime state.",
        "impact": "The agent must guess which state, data shape, or recent change matters, usually causing extra turns.",
        "better_next_time": "Include repro steps, expected vs actual behavior, and the relevant state/data shape.",
    },
}


def pattern_title(pattern):
    return PATTERN_EXPLAINERS.get(pattern, {}).get("title", pattern.replace("_", " ").title())


def _trim(text, limit=220):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _evidence_value(finding, prefix):
    for item in finding.get("evidence", []):
        if item.startswith(prefix):
            return item[len(prefix):]
    return ""


def _evidence_map(finding):
    values = {}
    for item in finding.get("evidence", []):
        if "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def _quote(text, limit=4000):
    text = _trim(text, limit)
    if not text:
        return "> (no message text)"
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _find_anchor_prompt(finding, prompts, tools):
    if not prompts:
        return None
    example = _evidence_value(finding, "example_prompt=")
    if example:
        needle = re.sub(r"\s+", " ", example[:90].lower()).strip()
        for prompt in prompts:
            haystack = re.sub(r"\s+", " ", prompt["text"].lower())
            if needle and needle in haystack:
                return prompt

    first_edit_index = next((tool["entry_index"] for tool in tools if tool["is_edit"]), None)
    if first_edit_index is not None:
        before_edit = [prompt for prompt in prompts if prompt["entry_index"] < first_edit_index]
        if before_edit:
            return before_edit[-1]
    return prompts[0]


def _prompt_window(prompts, anchor):
    if not prompts or not anchor:
        return [], []
    anchor_pos = next((i for i, prompt in enumerate(prompts)
                       if prompt["entry_index"] == anchor["entry_index"]), 0)
    return prompts[max(0, anchor_pos - 2):anchor_pos], prompts[anchor_pos + 1:anchor_pos + 3]


def _tools_after_anchor(tools, anchor, limit=12):
    start = anchor["entry_index"] if anchor else 0
    return [tool for tool in tools if tool["entry_index"] >= start][:limit]


def _finding_story(finding, anchor, tools):
    pattern = finding.get("pattern")
    evidence = _evidence_map(finding)
    if pattern == "missing_verification_criteria":
        edit_count = evidence.get("file_edit_count", "?")
        return (
            f"The transcript shows {edit_count} edit tool call(s), but no verification "
            "command and no review marker after the work. The issue is not that the "
            "change was definitely wrong; it is that the session lacks proof that it "
            "was checked."
        )
    if pattern == "kitchen_sink_session":
        context = evidence.get("context_pct", "?")
        boundaries = evidence.get("task_boundary_prompts", "?")
        return (
            f"The session was already carrying about {context} of the context window "
            f"and contained {boundaries} task-boundary prompt(s). The anchor message "
            "adds another concern instead of isolating it into a fresh task envelope."
        )
    if pattern == "edit_before_plan":
        edit_count = evidence.get("file_edit_count", "?")
        complex_count = evidence.get("complex_prompt_count", "?")
        return (
            f"The detector saw {edit_count} edit tool call(s), {complex_count} complex "
            "prompt marker(s), and no explicit planning marker before edits. That means "
            "implementation direction was chosen before the transcript captured a plan."
        )
    example = anchor["text"] if anchor else ""
    return f"The detector matched `{pattern}` from the transcript evidence around: {_trim(example, 240)}"


def _why_unoptimized(finding):
    pattern = finding.get("pattern")
    if pattern == "missing_verification_criteria":
        return (
            "Agentic coding often feels complete when the diff is produced, but the "
            "expensive failure mode is accepting plausible output without a fresh check. "
            "That creates downstream review and debugging cost."
        )
    if pattern == "kitchen_sink_session":
        return (
            "Large mixed sessions make the agent pay attention to stale context and "
            "unrelated constraints. They also make it harder for you to review what was "
            "decided, because one transcript contains several workstreams."
        )
    if pattern == "edit_before_plan":
        return (
            "For multi-step work, editing first forces the plan to be implicit. If the "
            "agent picked the wrong scope or sequence, the error appears later as rework "
            "instead of being caught at the cheaper planning checkpoint."
        )
    return "The workflow likely spent more context, attention, or review effort than necessary."


def _better_prompt_template(finding, anchor):
    pattern = finding.get("pattern")
    original = _trim(anchor["text"], 260) if anchor else "this task"
    if pattern == "missing_verification_criteria":
        return (
            "After editing, run the smallest relevant verification command. In your final "
            "answer, include: command run, pass/fail result, and any unverified areas. "
            "If no automated check exists, show the manual review evidence."
        )
    if pattern == "kitchen_sink_session":
        return (
            "Start a fresh task: `Goal: <one outcome>. Context: <only relevant files or "
            "constraints>. Do not edit yet. First give me the file list, risks, and done "
            "condition; wait for confirmation before implementation.`"
        )
    if pattern == "edit_before_plan":
        return (
            "Before editing, respond with: `Plan, files likely to change, files to avoid, "
            "verification command, open questions.` Wait for my confirmation if the scope "
            f"is broader than the original ask: {original}"
        )
    return "Make the target, constraints, and success signal explicit before asking the agent to continue."


def _write_prompt_list(lines, title, prompts, limit=1600):
    lines += [title]
    if not prompts:
        lines.append("- None captured.")
        return
    for prompt in prompts:
        timestamp = f" {prompt['timestamp']}" if prompt.get("timestamp") else ""
        lines.append(f"- Turn {prompt['turn']} entry {prompt['entry_index']}{timestamp}:")
        lines.append(_quote(prompt["text"], limit=limit))


def _write_tool_list(lines, title, tools):
    lines += [title]
    if not tools:
        lines.append("- None captured.")
        return
    for tool in tools:
        flags = []
        if tool["is_edit"]:
            flags.append("edit")
        if tool["is_read"]:
            flags.append("read")
        if tool["is_verification"]:
            flags.append("verification")
        suffix = f" ({', '.join(flags)})" if flags else ""
        timestamp = f" {tool['timestamp']}" if tool.get("timestamp") else ""
        lines.append(
            f"- Entry {tool['entry_index']}{timestamp}: `{tool['name']}`{suffix} — "
            f"{_trim(tool['input_summary'], 300)}"
        )


def write_findings_deep_dive(baseline, findings, generated_at, trace=None,
                             output_path=None):
    lines = [
        "# AIDE Findings Deep Dive",
        "",
        f"_Generated {generated_at} from {baseline.get('sessions_scanned', 0)} local Claude sessions._",
        "",
        "This document is intentionally detailed. It is meant for reviewing the actual developer-agent loop behind each finding: project, surrounding messages, tool behavior, why the flow was unoptimized, and what a better next prompt/workflow would look like.",
        "",
        "Caveat: this is transcript-derived evidence. It explains why the detector fired; it does not prove that the final code was wrong.",
        "",
    ]

    session_cache = {}
    total = len(findings)
    for i, finding in enumerate(findings, start=1):
        if trace and total:
            trace.progress(i, total, finding.get("session_id", ""))
        path = finding.get("session_path", "")
        if path not in session_cache:
            entries = read_session_entries(path)
            session_cache[path] = {
                "entries": entries,
                "prompts": user_prompt_timeline(entries),
                "tools": tool_timeline(entries),
            }
        context = session_cache[path]
        prompts = context["prompts"]
        tools = context["tools"]
        anchor = _find_anchor_prompt(finding, prompts, tools)
        before, after = _prompt_window(prompts, anchor)
        edits = [tool for tool in tools if tool["is_edit"]]
        verification = [tool for tool in tools if tool["is_verification"]]
        nearby_tools = _tools_after_anchor(tools, anchor)

        lines += [
            f"## {i}. {pattern_title(finding.get('pattern'))}",
            "",
            "### Summary",
            "",
            f"- Pattern: `{finding.get('pattern')}`",
            f"- Project: `{finding.get('project') or 'unknown'}`",
            f"- Git branch: `{finding.get('git_branch') or 'unknown'}`",
            f"- Session: `{finding.get('session_id')}`",
            f"- Source transcript: `{finding.get('session_path')}`",
            f"- Time window: `{finding.get('started_at') or 'unknown'}` to `{finding.get('ended_at') or 'unknown'}`",
            f"- Attribution: `{finding.get('attribution')}`",
            f"- Confidence / alert: `{finding.get('confidence')}` / `{finding.get('alert_level')}`",
            f"- Score: `{finding.get('score')}`",
            "",
            "### What Happened",
            "",
            _finding_story(finding, anchor, tools),
            "",
            "### Why This Was Unoptimized",
            "",
            _why_unoptimized(finding),
            "",
            "### What Could Have Been Better",
            "",
            _better_prompt_template(finding, anchor),
            "",
            "### Detector Evidence",
            "",
        ]
        for item in finding.get("evidence", []):
            lines.append(f"- `{_trim(item, 420)}`")

        lines += ["", "### Anchor Message", ""]
        if anchor:
            lines.append(f"Turn {anchor['turn']} entry {anchor['entry_index']} {anchor.get('timestamp', '')}".strip())
            lines.append(_quote(anchor["text"], limit=5000))
        else:
            lines.append("No user prompt anchor was captured.")

        lines += ["", "### Nearby User Messages", ""]
        _write_prompt_list(lines, "Before:", before)
        _write_prompt_list(lines, "After:", after)

        lines += ["", "### Full User Message Timeline For This Session", ""]
        _write_prompt_list(lines, "All user prompts:", prompts, limit=2200)

        lines += ["", "### Tool Context", ""]
        _write_tool_list(lines, "Tool calls after the anchor:", nearby_tools)
        _write_tool_list(lines, "Edit tool calls in the session:", edits)
        _write_tool_list(lines, "Verification-looking tool calls in the session:", verification)
        lines.append("")

    (output_path or FINDINGS_DEEP_DIVE).write_text("\n".join(lines))
    if trace:
        trace.progress_end()


def write_change_first(findings, generated_at, rulebook_escalation=None,
                       output_path=None):
    """Top-8 ranked interventions (extension doc §22 style)."""
    by_pattern = {}
    for finding in findings:
        pattern = finding.get("pattern")
        if pattern not in by_pattern or finding.get("score", 0) > by_pattern[pattern].get("score", 0):
            by_pattern[pattern] = finding

    top = sorted(by_pattern.values(),
                 key=lambda f: -f.get("score", 0))[:8]
    lines = [
        "# Change First",
        "",
        f"_Generated {generated_at}. Top patterns to fix first, ranked by "
        "impact score. Each is one paragraph with evidence numbers._",
        "",
    ]
    for i, finding in enumerate(top, start=1):
        evidence = "; ".join(finding.get("evidence", [])[:3])
        pattern = finding.get("pattern")
        esc = (rulebook_escalation or {}).get(pattern) if rulebook_escalation else None
        esc_line = ""
        if esc:
            if isinstance(esc, dict):
                ladder = esc.get("ladder", [])
                start = esc.get("start_rung", 0)
                esc_line = (f"\n**Escalation:** rung {start} → "
                            f"{' → '.join(ladder[start:])}")
            elif isinstance(esc, list):
                esc_line = f"\n**Escalation ladder:** {' → '.join(esc)}"
        lines += [
            f"## {i}. {pattern_title(finding.get('pattern'))}",
            "",
            f"**Pattern:** `{finding.get('pattern')}` "
            f"({finding.get('intervention_class')}, {finding.get('tier')})",
            "",
            finding.get("suggested_intervention", ""),
            esc_line,
            "",
            f"Evidence: {evidence or 'see findings report'}",
            "",
        ]
    (output_path or CHANGE_FIRST).write_text("\n".join(lines))


def write_findings_report(baseline, findings, generated_at, rulebook_escalation=None,
                          output_path=None):
    lines = [
        "# AIDE Findings Report",
        "",
        f"_Generated {generated_at} from {baseline.get('sessions_scanned', 0)} local Claude sessions._",
        "",
        "This report summarizes generic detector findings. It is a behavior profile, not blame: attribution means where the transcript evidence points (`developer`, `agent`, `tool_or_platform`, or `ambiguous`).",
        "",
        "## Overview",
        "",
        f"- Generic findings: **{len(findings)}**",
        f"- Personalized shortlist segments: **{baseline.get('segments_flagged', 0)}**",
        f"- Agent-recovered error streaks: **{baseline.get('agent_recovered_streaks', 0)}**",
    ]

    if baseline.get("by_pattern"):
        lines += ["", "### Counts By Pattern", ""]
        for pattern, count in sorted(baseline["by_pattern"].items(), key=lambda item: (-item[1], item[0])):
            rate = baseline.get("base_rates", {}).get(pattern)
            suffix = f" ({rate:.0%} of eligible sessions)" if rate is not None else ""
            lines.append(f"- **{pattern_title(pattern)}** (`{pattern}`): {count}{suffix}")

    if baseline.get("by_intervention_class"):
        lines += ["", "### Counts By Intervention Class", ""]
        for cls, count in sorted(baseline["by_intervention_class"].items()):
            lines.append(f"- `{cls}`: {count}")

    if baseline.get("by_attribution"):
        lines += ["", "### Counts By Attribution", ""]
        for attribution, count in sorted(baseline["by_attribution"].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{attribution}`: {count}")

    by_class = defaultdict(list)
    for finding in findings:
        by_class[finding.get("intervention_class", "unknown")].append(finding)

    lines += ["", "## Findings By Intervention Class", ""]
    for cls in sorted(by_class):
        lines += [f"### Class {cls}", ""]
        grouped = {}
        for finding in by_class[cls]:
            grouped.setdefault(finding.get("pattern", "unknown"), []).append(finding)
        for pattern, group in sorted(grouped.items(), key=lambda item: -len(item[1])):
            lines.append(f"- **{pattern_title(pattern)}** (`{pattern}`): {len(group)}")
        lines.append("")

    grouped = {}
    for finding in findings:
        grouped.setdefault(finding.get("pattern", "unknown"), []).append(finding)

    lines += ["", "## Behavior Patterns", ""]
    for pattern, group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        explainer = PATTERN_EXPLAINERS.get(pattern, {})
        lines += [
            f"### {pattern_title(pattern)}",
            "",
            f"- Pattern: `{pattern}`",
            f"- Findings: **{len(group)}**",
            f"- Attribution mix: {', '.join(f'`{k}` {v}' for k, v in sorted(count_by(group, 'attribution').items()))}",
            f"- Alert mix: {', '.join(f'`{k}` {v}' for k, v in sorted(count_by(group, 'alert_level').items()))}",
            f"- Behavior: {explainer.get('behavior', 'Detector-specific behavior captured by transcript evidence.')}",
            f"- Impact: {explainer.get('impact', 'Impact needs calibration against real sessions.')}",
            f"- Better next time: {explainer.get('better_next_time', group[0].get('suggested_intervention', 'Review the evidence and pick a lower-cost workflow.'))}",
            "",
        ]
        examples = group[:3]
        lines += ["Examples:"]
        for finding in examples:
            example = _evidence_value(finding, "example_prompt=")
            evidence = "; ".join(item for item in finding.get("evidence", []) if not item.startswith("example_prompt="))
            sample = example or evidence or finding.get("suggested_intervention", "")
            lines.append(
                f"- `{finding.get('session_id')}` score {finding.get('score')} "
                f"({finding.get('confidence')}, {finding.get('alert_level')}): {_trim(sample)}"
            )
        lines.append("")

    if rulebook_escalation:
        lines += ["", "## Escalation Ladders", "",
                  "_Progressive intervention channels per pattern. "
                  "Start rung advances when telemetry shows sustained ignores._", ""]
        for pattern, meta in sorted(rulebook_escalation.items()):
            if isinstance(meta, dict):
                ladder = meta.get("ladder", [])
                start = meta.get("start_rung", 0)
                telem = meta.get("telemetry") or {}
                lines.append(
                    f"- **{pattern_title(pattern)}** (`{pattern}`): "
                    f"start rung {start} → {' → '.join(ladder)} "
                    f"(fires={telem.get('fires', 0)}, overrides={telem.get('overrides', 0)})")
            elif isinstance(meta, list):
                lines.append(
                    f"- **{pattern_title(pattern)}** (`{pattern}`): {' → '.join(meta)}")
        lines.append("")

    lines += ["## All Findings", ""]
    for i, finding in enumerate(findings, start=1):
        lines += [
            f"### {i}. {pattern_title(finding.get('pattern'))}",
            "",
            f"- Session: `{finding.get('session_id')}`",
            f"- Pattern: `{finding.get('pattern')}`",
            f"- Score: `{finding.get('score')}`",
            f"- Attribution: `{finding.get('attribution')}`",
            f"- Confidence: `{finding.get('confidence')}`",
            f"- Alert level: `{finding.get('alert_level')}`",
            f"- Suggested intervention: {finding.get('suggested_intervention')}",
            "- Evidence:",
        ]
        for item in finding.get("evidence", []):
            lines.append(f"  - {_trim(item, 320)}")
        lines.append("")

    (output_path or FINDINGS_REPORT).write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--max-sessions", type=int, default=2000)
    ap.add_argument("--since", type=int, default=0,
                    help="Only sessions modified in the last N days (0 = all)")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="Suppress live progress trace (stderr)")
    ap.add_argument("--list-runs", action="store_true",
                    help="List archived runs and exit")
    args = ap.parse_args()

    if args.list_runs:
        for run in list_runs(DATA_DIR):
            print(f"{run['run_id']}\t"
                  f"sessions={run.get('sessions_scanned', '?')}\t"
                  f"segments={run.get('segments_flagged', '?')}\t"
                  f"{run.get('generated_at', '')}")
        return

    trace = MinerTrace(quiet=args.quiet)

    trace.stage(f"Discovering sessions under {args.root}")
    files = sorted(Path(args.root).rglob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if args.since:
        cutoff = time.time() - args.since * 86400
        files = [f for f in files if f.stat().st_mtime >= cutoff]
    files = files[:args.max_sessions]
    if not files:
        print(f"No session files under {args.root}", file=sys.stderr)
        sys.exit(1)
    trace.info(f"Found {len(files)} session file(s) to scan"
               + (f" (last {args.since} days)" if args.since else ""))
    if args.max_sessions < 50 and len(files) < 200:
        trace.info(f"WARNING: --max-sessions {args.max_sessions} — small runs "
                   "archive separately; use full default (2000) for production.")

    trace.stage("Scanning sessions (features, detectors, segments)")
    session_pairs = []
    session_findings = {}
    all_segments = []
    generic_findings = []
    token_totals, context_peaks = [], []
    agent_recovered_streaks = 0
    skipped = 0

    for i, f in enumerate(files, start=1):
        trace.progress(i, len(files), f.stem)
        pair = collect_session_pair(f)
        if pair is None:
            skipped += 1
            continue
        features, metadata = pair
        session_pairs.append(pair)
        findings = enrich_findings(run_generic_detectors(features), features, metadata)
        session_findings[features.session_id] = findings
        generic_findings.extend(findings)

        s = parse_session(f)
        if not s or not s["prompts"]:
            continue
        token_totals.append(s["total_tokens"])
        context_peaks.append(s["max_context"])
        segs = find_segments(f.stem, s)
        flagged_streaks = sum(1 for x in segs if x["signal"] == "error_streak")
        total_streaks = sum(1 for v in s["streak_after"].values() if v >= 3)
        agent_recovered_streaks += max(0, total_streaks - flagged_streaks)
        all_segments.extend(segs)

    trace.progress_end()
    trace.info(f"Parsed {len(session_pairs)} sessions"
               + (f", skipped {skipped}" if skipped else "")
               + f", {len(generic_findings)} generic findings, "
               f"{len(all_segments)} shortlist segments")

    trace.stage("Cross-session aggregation")
    cross = aggregate(session_pairs)
    cross_findings = cross["findings"]
    percentiles = calibrate(session_pairs)
    trace.info(f"{len(cross_findings)} cross-session findings, "
               f"{len(cross.get('template_signatures', []))} template signature(s)")

    trace.stage("Config lints")
    repo_paths = sorted({
        m["project"] for _, m in session_pairs
        if m.get("project") and Path(m["project"]).is_dir()
    })
    config_findings = run_config_lints(repo_paths, session_pairs)
    trace.info(f"{len(repo_paths)} repo(s), {len(config_findings)} config finding(s)")

    all_findings = generic_findings + cross_findings
    # D-patch findings go to config report only, not the main findings list
    report_findings = [f for f in all_findings
                       if f.get("intervention_class") != "D-patch"]
    report_findings.extend(config_findings)  # include in JSON export

    trace.stage("Compiling rulebook + escalation ladders")
    existing_rulebook = {}
    prior_rulebook = get_latest_run_dir() / "rulebook.json"
    if not prior_rulebook.exists():
        prior_rulebook = RULEBOOK_PATH
    if prior_rulebook.exists():
        try:
            existing_rulebook = json.loads(prior_rulebook.read_text())
        except json.JSONDecodeError:
            pass
    rulebook = compile_rulebook(
        cross, percentiles, existing_rulebook, telemetry_path=str(TELEMETRY_PATH),
        since_days=args.since)
    trace.info(f"{len(rulebook.get('patterns', []))} rulebook pattern(s), "
               f"{len(rulebook.get('escalation', {}))} escalation ladder(s)")

    n = len(token_totals)
    baseline = {
        "sessions_scanned": len(session_pairs),
        "avg_tokens_per_session": int(sum(token_totals) / n) if n else 0,
        "p90_context_peak": sorted(context_peaks)[int(0.9 * (len(context_peaks) - 1))] if context_peaks else 0,
        "segments_flagged": len(all_segments),
        "agent_recovered_streaks": agent_recovered_streaks,
        "by_signal": {},
        "generic_findings": len(generic_findings),
        "cross_session_findings": len(cross_findings),
        "config_findings": len(config_findings),
        "by_pattern": count_by(report_findings, "pattern"),
        "by_attribution": count_by(report_findings, "attribution"),
        "by_alert_level": count_by(report_findings, "alert_level"),
        "by_intervention_class": count_by(report_findings, "intervention_class"),
        "percentiles": percentiles,
        "base_rates": compute_base_rates(session_pairs, report_findings),
    }
    for seg in all_segments:
        baseline["by_signal"][seg["signal"]] = baseline["by_signal"].get(seg["signal"], 0) + 1

    trace.stage("Writing exports")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    run_id, run_dir = new_run_dir(DATA_DIR)

    deep_dive_findings = [f for f in report_findings if f.get("session_path")]
    trace.info(f"archive {run_dir.name} — shortlist, findings, reports, "
               f"rulebook ({len(deep_dive_findings)} deep dives)")

    (run_dir / "shortlist.json").write_text(json.dumps({
        "detector_version": DETECTOR_VERSION,
        "generated_at": generated_at,
        "segments": all_segments,
    }, indent=1))
    (run_dir / "findings.json").write_text(json.dumps({
        "detector_version": DETECTOR_VERSION,
        "generated_at": generated_at,
        "findings": report_findings,
    }, indent=1))
    write_findings_report(
        baseline, report_findings, generated_at,
        rulebook_escalation=rulebook.get("escalation_meta"),
        output_path=run_dir / "findings_report.md")
    write_findings_deep_dive(
        baseline, deep_dive_findings, generated_at, trace=trace,
        output_path=run_dir / "findings_deep_dive.md")
    write_change_first(
        report_findings, generated_at,
        rulebook_escalation=rulebook.get("escalation_meta"),
        output_path=run_dir / "change_first.md")
    (run_dir / "config_report.md").write_text(
        render_config_report(config_findings, generated_at))
    (run_dir / "rulebook.json").write_text(json.dumps(rulebook, indent=1))
    (run_dir / "baseline.json").write_text(json.dumps(baseline, indent=1))

    dashboard = build_dashboard_export(
        generated_at=generated_at,
        detector_version=DETECTOR_VERSION,
        session_pairs=session_pairs,
        session_findings=session_findings,
        cross_session_findings=cross_findings,
        config_findings=config_findings,
        aggregates=cross,
        percentiles=percentiles,
        baseline=baseline,
        escalation_meta=rulebook.get("escalation_meta"),
    )
    write_dashboard_export(run_dir / "dashboard_data.json", dashboard)

    run_meta = {
        "run_id": run_id,
        "generated_at": generated_at,
        "sessions_scanned": len(session_pairs),
        "segments_flagged": len(all_segments),
        "generic_findings": len(generic_findings),
        "max_sessions": args.max_sessions,
        "since_days": args.since,
    }
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=1))
    latest = finalize_run(run_id, run_dir, run_meta, DATA_DIR)

    trace.finish()
    print(json.dumps(baseline, indent=2))
    print(f"\nArchived run → {run_dir}")
    print(f"Latest pointer → {DATA_DIR / 'latest.json'} ({latest['run_id']})")
    print(f"Shortlist written to {run_dir / 'shortlist.json'}")
    print(f"Findings written to {run_dir / 'findings.json'}")
    print(f"Findings report written to {run_dir / 'findings_report.md'}")
    print(f"Findings deep dive written to {run_dir / 'findings_deep_dive.md'}")
    print(f"Change-first written to {run_dir / 'change_first.md'}")
    print(f"Config report written to {run_dir / 'config_report.md'}")
    print(f"Dashboard data written to {run_dir / 'dashboard_data.json'}")
    print(f"Rulebook published to {DATA_DIR / 'rulebook.json'} (from archive)")
    print("Next: python3 label.py")
    print(f"List past runs: python3 mine.py --list-runs")


if __name__ == "__main__":
    main()

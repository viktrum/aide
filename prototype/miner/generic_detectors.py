import re

try:
    from session_features import SessionFeatures
except ImportError:  # pragma: no cover - package import fallback
    from .session_features import SessionFeatures


ATTRIBUTIONS = {"developer", "agent", "tool_or_platform", "ambiguous"}
CONFIDENCES = {"low", "medium", "high"}
ALERT_LEVELS = {"log_only", "passive_insight", "strong_nudge"}
# Intervention classes (the AIDE design notes §3):
#   A = prompt-intrinsic, B = session-state gate, C = retrospective-preventive,
#   D-report / D-patch = not prompt-deliverable.
INTERVENTION_CLASSES = {"A", "B", "C", "D-report", "D-patch"}
TIERS = {"T0", "T1"}

# Thresholds (module constants so the rulebook compiler can override them
# from per-user baseline percentiles later).
RETRY_SPIRAL_MIN_RUN = 3
MARATHON_MINUTES = 1440          # 24 hours
MARATHON_PROMPTS = 60
MARATHON_COMPACTIONS = 2
QUESTION_STACK_MIN = 3
QUESTION_STACK_MAX_WORDS = 200
INTERRUPT_CHURN_MIN = 3
CONTEXT_STUFF_ABS_TOKENS = 150_000
CONTEXT_STUFF_MIN_PREV_TOKENS = 20_000
WEBFETCH_CHAIN_MIN = 12

# Legitimate polling commands are not retry spirals.
POLL_CMD_RE = re.compile(r"\b(sleep|tail\s+-f|watch)\b")
# R22 canonical — risky destructive commands.
RISKY_CMD_RE = re.compile(
    r"\b(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fd|drop\s+database|"
    r"truncate\s+table|delete\s+from|terraform\s+apply|kubectl\s+apply|"
    r"kubectl\s+delete|docker\s+rm|docker\s+system\s+prune|chmod\s+777|"
    r"chown\s+-R)\b",
    re.I,
)


TASK_BOUNDARY_RE = re.compile(
    r"\b("
    r"also|additionally|plus|while\s+you(?:'re| are)\s+there|"
    r"one\s+more\s+thing|another\s+thing|by\s+the\s+way|btw|"
    r"switching\s+gears|different\s+topic|unrelated|"
    r"and\s+then|after\s+that|then\s+also"
    r")\b",
    re.I,
)
CORRECTION_RE = re.compile(
    r"(^|\b)("
    r"no,\s+i\s+meant|"
    r"that(?:'s| is)\s+(?:wrong|incorrect|not\s+(?:right|correct))|"
    r"not\s+what\s+i\s+(?:asked|wanted|meant)|"
    r"you\s+(?:missed|forgot|broke|ignored)|"
    r"still\s+(?:broken|failing|wrong)|"
    r"wrong\s+(?:file|component|function|module|branch|target)|"
    r"try\s+again|"
    r"why\s+did\s+you|"
    r"come\s+on"
    r")\b",
    re.I,
)
ERROR_EVIDENCE_RE = re.compile(
    r"\b("
    r"error|exception|traceback|typeerror|referenceerror|syntaxerror|"
    r"undefined|null|cannot\s+read|crash(?:es|ed|ing)?|"
    r"stack\s+trace"
    r")\b",
    re.I,
)
ERROR_FAILURE_RE = re.compile(
    r"\b("
    r"(?:test|build|command|pipeline|job|deploy|ci|server|app|page)\s+"
    r"(?:failed|failing)|"
    r"(?:failed|failing)\s+"
    r"(?:test|build|command|pipeline|job|deploy|ci|server|app|page)"
    r")\b",
    re.I,
)
ERROR_CONTEXT_RE = re.compile(
    r"\b("
    r"expected|actual|repro(?:duce|duction)?|steps?\s+to\s+reproduce|"
    r"initial\s+state|state|props?|api\s+response|response\s+(?:body|payload|shape)|"
    r"data\s+shape|schema|recent\s+change|changed\s+recently|"
    r"file|component"
    r")\b",
    re.I,
)
TYPO_CORRECTION_RE = re.compile(
    r"\b("
    r"i\s+meant|mistyped|misspelled|my\s+bad|sry\s+meant|"
    r"typo[,:\s]+i\s+meant"
    r")\b",
    re.I,
)


def run_generic_detectors(features) -> list[dict]:
    """Run generic detector registry against a completed session feature record."""
    findings = []
    for detector in DETECTORS:
        finding = detector(features)
        if finding:
            findings.append(finding)
    return findings


def _finding(
    pattern,
    score,
    evidence,
    attribution,
    confidence,
    alert_level,
    suggested_intervention,
    intervention_class="B",
    tier="T0",
):
    return {
        "pattern": pattern,
        "score": round(float(score), 3),
        "evidence": [str(item) for item in evidence if str(item)],
        "attribution": attribution if attribution in ATTRIBUTIONS else "ambiguous",
        "confidence": confidence if confidence in CONFIDENCES else "low",
        "alert_level": alert_level if alert_level in ALERT_LEVELS else "log_only",
        "suggested_intervention": str(suggested_intervention),
        "intervention_class": (
            intervention_class if intervention_class in INTERVENTION_CLASSES else "D-report"
        ),
        "tier": tier if tier in TIERS else "T1",
    }


def _prompts(features):
    return list(getattr(features, "user_prompts", []) or [])


def _prompt_author_attribution(features, default):
    session_id = getattr(features, "session_id", "") or ""
    return "agent" if session_id.startswith("agent-") else default


def _prompt_text(prompt):
    return getattr(prompt, "text_norm", "") or getattr(prompt, "text", "") or ""


def _prompt_excerpt(prompt, max_chars=140):
    text = (getattr(prompt, "text", "") or _prompt_text(prompt)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _has_clear_marker(features):
    return getattr(features, "clear_or_compact_marker_count", 0) > 0


def _complex_prompt_count(features):
    count = 0
    for prompt in _prompts(features):
        if getattr(prompt, "is_status_ack", False):
            continue
        word_count = getattr(prompt, "word_count", 0)
        imperative_count = getattr(prompt, "imperative_count", 0)
        has_file_ref = getattr(prompt, "has_file_ref", False)
        has_verify_marker = getattr(prompt, "has_verify_marker", False)
        has_review_marker = getattr(prompt, "has_review_marker", False)
        has_task_boundary = bool(TASK_BOUNDARY_RE.search(_prompt_text(prompt)))
        if (
            word_count >= 35
            or imperative_count >= 3
            or (imperative_count >= 2 and (has_file_ref or has_verify_marker or has_review_marker))
            or has_task_boundary
        ):
            count += 1
    return count


def _detector_missing_verification_criteria(features: SessionFeatures):
    edit_count = getattr(features, "file_edit_count", 0)
    verification_count = getattr(features, "verification_command_count", 0)
    review_count = getattr(features, "review_marker_count", 0)
    if edit_count <= 0 or verification_count != 0 or review_count != 0:
        return None

    alert_level = "strong_nudge" if edit_count >= 2 else "passive_insight"
    confidence = "high" if edit_count >= 2 else "medium"
    return _finding(
        "missing_verification_criteria",
        0.58 + min(edit_count, 5) * 0.06,
        [
            f"file_edit_count={edit_count}",
            "verification_command_count=0",
            "review_marker_count=0",
        ],
        "agent",
        confidence,
        alert_level,
        "Before closing the task, ask for or run an explicit verification command and capture the expected success signal.",
        intervention_class="B",
        tier="T0",
    )


def _detector_repo_context_dumping(features: SessionFeatures):
    if getattr(features, "file_edit_count", 0) <= 0:
        return None

    files_before_edit = getattr(features, "files_read_before_first_edit", 0)
    output_before_edit = getattr(features, "tool_output_chars_before_first_edit", 0)
    if files_before_edit <= 25 and output_before_edit <= 50_000:
        return None

    evidence = []
    if files_before_edit > 25:
        evidence.append(f"files_read_before_first_edit={files_before_edit} > 25")
    if output_before_edit > 50_000:
        evidence.append(f"tool_output_chars_before_first_edit={output_before_edit} > 50000")
    return _finding(
        "repo_context_dumping",
        0.62 + min(len(evidence), 2) * 0.08,
        evidence,
        "agent",
        "high",
        "passive_insight",
        "Start with a narrower file or symbol hypothesis, then expand reads only when the first pass falsifies it.",
        intervention_class="B",
        tier="T0",
    )


def _detector_kitchen_sink_session(features: SessionFeatures):
    if _has_clear_marker(features) or getattr(features, "context_pct", 0.0) < 0.40:
        return None

    boundary_prompts = [
        prompt for prompt in _prompts(features)
        if not getattr(prompt, "is_status_ack", False)
        and TASK_BOUNDARY_RE.search(_prompt_text(prompt))
    ]
    if not boundary_prompts:
        return None

    evidence = [
        f"context_pct={getattr(features, 'context_pct', 0.0):.2f}",
        f"task_boundary_prompts={len(boundary_prompts)}",
        f"example_prompt={_prompt_excerpt(boundary_prompts[0])}",
    ]
    return _finding(
        "kitchen_sink_session",
        0.64 if len(boundary_prompts) == 1 else 0.72,
        evidence,
        _prompt_author_attribution(features, "developer"),
        "medium",
        "passive_insight",
        "Split the next unrelated request into a fresh or compacted session with a concise goal statement.",
        intervention_class="B",
        tier="T0",
    )


def _detector_correction_accumulation(features: SessionFeatures):
    if _has_clear_marker(features):
        return None

    correction_prompts = [
        prompt for prompt in _prompts(features)
        if not getattr(prompt, "is_status_ack", False)
        and CORRECTION_RE.search(_prompt_text(prompt))
    ]
    if len(correction_prompts) < 2:
        return None

    alert_level = "strong_nudge" if len(correction_prompts) >= 3 else "passive_insight"
    confidence = "high" if len(correction_prompts) >= 3 else "medium"
    return _finding(
        "correction_accumulation",
        0.58 + min(len(correction_prompts), 5) * 0.07,
        [
            f"correction_prompt_count={len(correction_prompts)}",
            f"first_example={_prompt_excerpt(correction_prompts[0])}",
            f"latest_example={_prompt_excerpt(correction_prompts[-1])}",
            "clear_or_compact_marker_count=0",
        ],
        _prompt_author_attribution(features, "ambiguous"),
        confidence,
        alert_level,
        "Pause for a short restatement of the intended outcome, constraints, and what has already been tried.",
        intervention_class="B",
        tier="T0",
    )


def _detector_edit_before_plan(features: SessionFeatures):
    edit_count = getattr(features, "file_edit_count", 0)
    planning_count = getattr(features, "planning_marker_count", 0)
    complex_count = _complex_prompt_count(features)
    if edit_count <= 0 or planning_count != 0 or complex_count <= 0:
        return None

    confidence = "high" if edit_count >= 3 or complex_count >= 2 else "medium"
    alert_level = "strong_nudge" if edit_count >= 3 and complex_count >= 2 else "passive_insight"
    return _finding(
        "edit_before_plan",
        0.60 + min(edit_count, 4) * 0.04 + min(complex_count, 3) * 0.04,
        [
            f"file_edit_count={edit_count}",
            "no user planning marker observed before edits",
            "planning_marker_count=0",
            f"complex_prompt_count={complex_count}",
        ],
        _prompt_author_attribution(features, "ambiguous"),
        confidence,
        alert_level,
        "For multi-step or ambiguous work, establish a brief plan marker or clarifying checkpoint before edits begin.",
        intervention_class="C",
        tier="T0",
    )


def _detector_vague_underprompting(features: SessionFeatures):
    vague_prompts = [
        prompt for prompt in _prompts(features)
        if getattr(prompt, "is_vague", False)
        and not getattr(prompt, "is_status_ack", False)
        and getattr(prompt, "word_count", 0) <= 8
    ]
    if not vague_prompts:
        return None

    return _finding(
        "vague_underprompting",
        0.58 + min(len(vague_prompts), 3) * 0.05,
        [
            f"short_vague_prompt_count={len(vague_prompts)}",
            f"example_prompt={_prompt_excerpt(vague_prompts[0])}",
        ],
        _prompt_author_attribution(features, "developer"),
        "medium" if len(vague_prompts) > 1 else "low",
        "passive_insight",
        "Ask for the target file, expected behavior, and a concrete done condition before proceeding.",
        intervention_class="A",
        tier="T0",
    )


def _detector_typo_induced_ambiguity(features: SessionFeatures):
    typo_prompts = [
        prompt for prompt in _prompts(features)
        if getattr(prompt, "has_typo_marker", False)
        and TYPO_CORRECTION_RE.search(_prompt_text(prompt))
    ]
    if not typo_prompts:
        return None

    paired_correction = any(
        CORRECTION_RE.search(_prompt_text(prompt))
        or TYPO_CORRECTION_RE.search(_prompt_text(prompt))
        for prompt in typo_prompts
    )
    return _finding(
        "typo_induced_ambiguity",
        0.55 + min(len(typo_prompts), 3) * 0.06 + (0.08 if paired_correction else 0.0),
        [
            f"typo_marker_count={len(typo_prompts)}",
            f"example_prompt={_prompt_excerpt(typo_prompts[0])}",
            f"paired_with_correction_language={paired_correction}",
        ],
        _prompt_author_attribution(features, "developer"),
        "medium" if paired_correction else "low",
        "strong_nudge" if paired_correction else "passive_insight",
        "Confirm the corrected term or target before continuing work that depends on the ambiguous wording.",
        intervention_class="A",
        tier="T0",
    )


def _detector_error_dump_without_repro_or_runtime_context(features: SessionFeatures):
    matching_prompts = []
    for prompt in _prompts(features):
        text = _prompt_text(prompt)
        if (
            (ERROR_EVIDENCE_RE.search(text) or ERROR_FAILURE_RE.search(text))
            and not ERROR_CONTEXT_RE.search(text)
        ):
            matching_prompts.append(prompt)

    if not matching_prompts:
        return None

    first_prompt = matching_prompts[0]
    first_match = (
        ERROR_EVIDENCE_RE.search(_prompt_text(first_prompt))
        or ERROR_FAILURE_RE.search(_prompt_text(first_prompt))
    )
    evidence = [
        f"error_prompt_count={len(matching_prompts)}",
        f"error_evidence={first_match.group(0) if first_match else 'error'}",
        "missing_context_markers=expected/actual/repro/state/props/API response/data shape/recent change/file/component",
        f"example_prompt={_prompt_excerpt(first_prompt)}",
    ]
    return _finding(
        "error_dump_without_repro_or_runtime_context",
        0.66,
        evidence,
        _prompt_author_attribution(features, "developer"),
        "medium",
        "passive_insight",
        "Ask for reproduction steps, expected versus actual behavior, and the relevant runtime state or data shape.",
        intervention_class="A",
        tier="T0",
    )


def _longest_bash_run(commands):
    """Longest run of consecutive identical normalized Bash commands.

    Returns (run_length, command, any_after_error). Polling commands
    (sleep/tail -f/watch) never count.
    """
    best_len, best_cmd, best_err = 1, "", False
    run_len, run_err = 1, False
    for prev, curr in zip(commands, commands[1:]):
        same = (
            curr["norm"]
            and curr["norm"] == prev["norm"]
            and not POLL_CMD_RE.search(curr["norm"])
        )
        if same:
            run_len += 1
            run_err = run_err or curr["after_error"] or prev["after_error"]
            if run_len > best_len:
                best_len, best_cmd, best_err = run_len, curr["norm"], run_err
        else:
            run_len, run_err = 1, False
    return best_len, best_cmd, best_err


def _detector_command_retry_spiral(features: SessionFeatures):
    commands = getattr(features, "bash_commands", []) or []
    if len(commands) < RETRY_SPIRAL_MIN_RUN:
        return None
    run_len, command, after_error = _longest_bash_run(commands)
    if run_len < RETRY_SPIRAL_MIN_RUN:
        return None

    return _finding(
        "command_retry_spiral",
        min(0.6 + 0.05 * run_len, 0.85),
        [
            f"consecutive_identical_bash_run={run_len}",
            f"command={command[:120]}",
            f"retry_after_error={after_error}",
        ],
        "agent",
        "high" if after_error else "medium",
        "passive_insight",
        "The same command ran repeatedly without an intervening change. Put the missing environment context (venv, package manager, paths) into CLAUDE.md so the agent stops probing.",
        intervention_class="B",
        tier="T0",
    )


def _detector_marathon_session_sprawl(features: SessionFeatures):
    if getattr(features, "is_subagent", False):
        return None
    if getattr(features, "clear_command_count", 0) != 0:
        return None

    minutes = getattr(features, "wall_clock_minutes", 0.0)
    prompts = getattr(features, "user_prompt_count", 0)
    compactions = getattr(features, "compaction_continuation_count", 0)
    triggers = []
    if minutes > MARATHON_MINUTES:
        triggers.append(f"wall_clock_hours={minutes / 60:.1f} > {MARATHON_MINUTES // 60}")
    if prompts > MARATHON_PROMPTS:
        triggers.append(f"user_prompt_count={prompts} > {MARATHON_PROMPTS}")
    if compactions >= MARATHON_COMPACTIONS:
        triggers.append(f"auto_compaction_continuations={compactions} >= {MARATHON_COMPACTIONS}")
    if not triggers:
        return None

    return _finding(
        "marathon_session_sprawl",
        0.6 + 0.08 * len(triggers),
        triggers + ["clear_command_count=0"],
        "developer",
        "high" if len(triggers) >= 2 else "medium",
        "passive_insight",
        "This session has crossed a day/N-task boundary. Cut a handoff brief and start fresh; long sessions pay compaction tax repeatedly and lose constraints each time.",
        intervention_class="B",
        tier="T0",
    )


def _detector_question_stacking(features: SessionFeatures):
    stacked = [
        prompt for prompt in _prompts(features)
        if getattr(prompt, "question_mark_count", 0) >= QUESTION_STACK_MIN
        and getattr(prompt, "word_count", 0) < QUESTION_STACK_MAX_WORDS
        and not getattr(prompt, "is_status_ack", False)
    ]
    if not stacked:
        return None

    return _finding(
        "question_stacking",
        0.55 + min(len(stacked), 4) * 0.05,
        [
            f"stacked_question_prompt_count={len(stacked)}",
            f"example_prompt={_prompt_excerpt(stacked[0])}",
        ],
        _prompt_author_attribution(features, "developer"),
        "medium" if len(stacked) >= 3 else "low",
        "passive_insight",
        "Stack at most two questions per prompt, or mark them as a numbered checklist so unanswered items are visible.",
        intervention_class="A",
        tier="T0",
    )


def _detector_interrupt_churn(features: SessionFeatures):
    indexes = getattr(features, "interrupt_entry_indexes", []) or []
    if len(indexes) < INTERRUPT_CHURN_MIN:
        return None

    # Guardrail: an interrupt whose next real prompt is a correction or scope
    # guard is effective supervision, not churn.
    prompts = _prompts(features)
    churn = 0
    for idx in indexes:
        next_prompt = next(
            (p for p in prompts if getattr(p, "entry_index", 0) > idx), None)
        if next_prompt and CORRECTION_RE.search(_prompt_text(next_prompt)):
            continue
        churn += 1
    if churn < INTERRUPT_CHURN_MIN:
        return None

    return _finding(
        "interrupt_churn",
        0.55 + min(churn, 5) * 0.05,
        [
            f"interrupt_count={len(indexes)}",
            f"non_corrective_interrupts={churn}",
        ],
        _prompt_author_attribution(features, "developer"),
        "medium",
        "passive_insight",
        "If you often interrupt to redirect, front-load the constraint that keeps triggering the interrupt.",
        intervention_class="B",
        tier="T0",
    )


def _detector_unattended_long_run(features: SessionFeatures):
    away = getattr(features, "away_summary_count", 0)
    if away < 1:
        return None

    risky = [
        cmd["norm"][:120] for cmd in getattr(features, "bash_commands", []) or []
        if RISKY_CMD_RE.search(cmd["norm"])
    ]
    escalate = getattr(features, "auto_mode_active", False) and bool(risky)
    evidence = [f"away_summary_count={away}"]
    if escalate:
        evidence.append("permission_mode=auto")
        evidence.append(f"risky_commands={risky[:3]}")
    return _finding(
        "unattended_long_run",
        0.7 if escalate else 0.5,
        evidence,
        "developer",
        "high" if escalate else "low",
        "passive_insight" if escalate else "log_only",
        "For unattended runs, pre-commit to a checkpoint plan: tests must pass, diff size cap, no dependency installs without a gate.",
        intervention_class="B",
        tier="T0",
    )


def _detector_context_stuffing_single_turn(features: SessionFeatures):
    series = getattr(features, "cache_read_series", []) or []
    jump = None
    for prev, curr in zip(series, series[1:]):
        if prev > CONTEXT_STUFF_MIN_PREV_TOKENS and curr > 2 * prev:
            jump = (prev, curr)
            break
    peak = max(series, default=0)
    if jump is None and peak <= CONTEXT_STUFF_ABS_TOKENS:
        return None

    evidence = []
    if jump:
        evidence.append(f"cache_read_jump={jump[0]}->{jump[1]}")
    if peak > CONTEXT_STUFF_ABS_TOKENS:
        evidence.append(f"cache_read_peak={peak} > {CONTEXT_STUFF_ABS_TOKENS}")
    return _finding(
        "context_stuffing_single_turn",
        0.6 + 0.1 * len(evidence),
        evidence,
        "ambiguous",
        "medium",
        "passive_insight",
        "Use offset/limit reads, grep with context lines, or a subagent to digest large files and return a summary.",
        intervention_class="B",
        tier="T0",
    )


def _detector_webfetch_chain(features: SessionFeatures):
    runs = [r for r in getattr(features, "web_tool_runs", []) or []
            if r >= WEBFETCH_CHAIN_MIN]
    if not runs:
        return None

    return _finding(
        "webfetch_chain",
        0.55 + min(max(runs), 6) * 0.04,
        [f"consecutive_web_tool_runs={sorted(runs, reverse=True)[:3]}"],
        "agent",
        "medium",
        "passive_insight",
        "Batch the fetches into one request or delegate research to a subagent that returns a synthesis.",
        intervention_class="B",
        tier="T0",
    )


def _detector_dangerous_auto_accept(features: SessionFeatures):
    if not getattr(features, "auto_mode_active", False):
        return None
    risky = [
        cmd["norm"][:120] for cmd in getattr(features, "bash_commands", []) or []
        if RISKY_CMD_RE.search(cmd["norm"])
    ]
    if not risky:
        return None

    return _finding(
        "dangerous_auto_accept",
        0.65 + min(len(risky), 3) * 0.05,
        [
            "permission_mode=auto",
            f"risky_command_count={len(risky)}",
            f"examples={risky[:3]}",
        ],
        "developer",
        "high",
        "strong_nudge",
        "Avoid global auto-accept while risky commands run. Auto-approve safe reads/tests only; keep destructive actions gated.",
        intervention_class="B",
        tier="T0",
    )


def _detector_manual_handoff_marker(features: SessionFeatures):
    """Session-level feeder for the cross-session manual_handoff_ritual
    pattern (6.55). One session is only weak evidence; cross_session.py
    aggregates these into the real finding."""
    handoff_prompts = [
        prompt for prompt in _prompts(features)
        if getattr(prompt, "has_handoff_marker", False)
    ]
    if not handoff_prompts:
        return None
    if "/handover" in (getattr(features, "slash_commands", []) or []):
        return None

    return _finding(
        "manual_handoff_marker",
        0.4,
        [
            f"handoff_prompt_count={len(handoff_prompts)}",
            f"example_prompt={_prompt_excerpt(handoff_prompts[0])}",
            "handover_slash_command_used=False",
        ],
        _prompt_author_attribution(features, "developer"),
        "low",
        "log_only",
        "You write handoff briefs manually. Codify the ritual as a /handover command or skill with a fixed template, and have it update project memory in the same pass.",
        intervention_class="C",
        tier="T0",
    )


DETECTORS = [
    _detector_missing_verification_criteria,
    _detector_repo_context_dumping,
    _detector_kitchen_sink_session,
    _detector_correction_accumulation,
    _detector_edit_before_plan,
    _detector_vague_underprompting,
    _detector_typo_induced_ambiguity,
    _detector_error_dump_without_repro_or_runtime_context,
    _detector_command_retry_spiral,
    _detector_marathon_session_sprawl,
    _detector_question_stacking,
    _detector_interrupt_churn,
    _detector_unattended_long_run,
    _detector_context_stuffing_single_turn,
    _detector_webfetch_chain,
    _detector_dangerous_auto_accept,
    _detector_manual_handoff_marker,
]


__all__ = ["run_generic_detectors"]

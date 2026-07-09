"""Rulebook v2 compiler.

Turns offline aggregates (cross_session.aggregate) + per-user percentiles
(cross_session.calibrate) into the rulebook the prompt judge evaluates.

Contract preserved from v1: `patterns[]` entries keep
trigger/action/rights/user_status so label.py-mined patterns keep working.
v2 adds: version, thresholds, template_signatures, correction_clusters,
handoff, and per-pattern class/cooldown/priority.

Precision rule (prototype/README.md): nothing compiled here is auto-promoted
to notices. Class-C rules start as user_status "candidate", which the judge
treats as inject-only until the user flips them to "confirmed".
"""
import time

# Signal vocabulary the judge exposes (compute_signals keys + Phase 6 additions).
JUDGE_SIGNAL_VOCAB = {
    "context_tokens", "context_window", "context_pct", "task_boundary",
    "last_cache_read", "idle_seconds", "session_has_file_edits", "turns",
    "consecutive_error_turns", "recent_corrections", "prompt_word_count",
    "prompt_has_file_ref", "prompt_imperative_asks",
    "prompt_similarity_to_failed", "prompt_similarity_to_recent",
    "session_has_test_evidence", "is_first_prompt", "claude_md_present",
    "is_git_repo",
    # v2 signals
    "hours_since_last_event", "session_wall_clock_hours",
    "compaction_continuation_seen", "prompt_question_marks",
    "prompt_matches_template", "prompt_matches_handoff",
    "prompt_matches_correction_cluster", "last_run_retry_spiral",
    "last_run_error_count", "prompt_mentions_internal_tool_names",
}
VALID_OPS = {">=", ">", "<=", "<", "=="}
VALID_CHANNELS = {"inject", "stdout", "block"}
VALID_CLASSES = {"A", "B", "C"}
VALID_PRIORITIES = {"risk", "waste", "quality"}

DEFAULT_THRESHOLDS = {
    "boundary_context_pct": 0.4,
    "stale_hours": 12,
    "stale_carried_tokens": 20_000,
    "marathon_hours": 24,
    "web_chain_min": 12,
    "reads_before_edit_min": 25,
    "stuffing_peak_cache": 150_000,
}

# Progressive escalation ladders (Phase 3). Channels are consumed by the judge
# (transform/inject/stdout/block) and V2 hooks (pretool). Index advances on
# re-mine when telemetry shows sustained ignore rates. Must stay in sync with
# prompt_judge.DEFAULT_ESCALATION (drift test in test_rulebook_compiler.py).
DEFAULT_ESCALATION = {
    "webfetch_chain": ["inject", "inject", "stdout", "pretool"],
    "repo_context_dumping": ["inject", "inject", "stdout"],
    "context_stuffing_single_turn": ["inject", "inject", "stdout"],
    "error_without_repro": ["transform", "inject", "stdout"],
    "edit_before_plan": ["stdout", "stdout", "inject"],
    "retry_same_prompt": ["transform", "transform", "stdout"],
    "missing_verification": ["inject", "inject", "stdout"],
}

# Legacy → current channel migration applied to prior ladders on re-mine.
LEGACY_CHANNELS = {"block": "transform"}

RULE_TO_PATTERN = {
    "R18": "webfetch_chain",
    "R19": "repo_context_dumping",
    "R20": "context_stuffing_single_turn",
    "R21": "error_without_repro",
    "R22": "edit_before_plan",
    "R2": "retry_same_prompt",
    "R7": "missing_verification",
}

TEMPLATE_MIN_SUPPORT = 10
HANDOFF_MIN_SUPPORT = 5
CLUSTER_MIN_SESSIONS = 3


def validate_rule(rule):
    """Mirror of label.validate_candidate for compiled entries.
    Returns an error string or None."""
    if not rule.get("id"):
        return "missing id"
    if rule.get("class") not in VALID_CLASSES:
        return f"bad class: {rule.get('class')!r}"
    conds = (rule.get("trigger") or {}).get("all")
    if not isinstance(conds, list) or not conds:
        return "trigger missing"
    for cond in conds:
        if cond.get("signal") not in JUDGE_SIGNAL_VOCAB:
            return f"unknown signal: {cond.get('signal')!r}"
        if cond.get("op") not in VALID_OPS:
            return f"unknown op: {cond.get('op')!r}"
        if not isinstance(cond.get("value"), (int, float)):
            return f"non-numeric value: {cond.get('value')!r}"
    if all(cond["op"] in (">=", ">") and cond["value"] <= 0 for cond in conds):
        return "trigger is always true (fails selectivity)"
    action = rule.get("action") or {}
    if action.get("channel") not in VALID_CHANNELS:
        return f"bad channel: {action.get('channel')!r}"
    message = action.get("message", "")
    if not (10 <= len(message) <= 400):
        return f"message length {len(message)} outside 10-400"
    if action.get("channel") == "block" and not (rule.get("rights") or {}).get("blocking"):
        return "block channel without blocking rights"
    return None


def compile_thresholds(percentiles):
    """Doc defaults, widened by the user's own baselines where those are
    higher (thresholds only ever get more permissive from calibration)."""
    thresholds = dict(DEFAULT_THRESHOLDS)
    wall_clock = (percentiles or {}).get("session_wall_clock_minutes") or {}
    p95_minutes = wall_clock.get("p95") or 0
    thresholds["marathon_hours"] = max(
        DEFAULT_THRESHOLDS["marathon_hours"], round(p95_minutes / 60, 1))
    return thresholds


def read_telemetry_escalation(telemetry_path):
    """Aggregate fires and overrides per pattern from judge telemetry."""
    from collections import defaultdict
    from pathlib import Path
    import json

    counts = defaultdict(lambda: {"fires": 0, "overrides": 0, "ignored": 0})
    try:
        for line in Path(telemetry_path).read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            pattern = rec.get("pattern")
            if not pattern:
                src = rec.get("source")
                pattern = RULE_TO_PATTERN.get(src)
            if not pattern:
                continue
            if rec.get("response") == "override":
                counts[pattern]["overrides"] += 1
            elif rec.get("channel") in ("inject", "stdout", "block", "deny"):
                counts[pattern]["fires"] += 1
            if rec.get("suppressed"):
                counts[pattern]["ignored"] += 1
    except (OSError, json.JSONDecodeError):
        pass
    return dict(counts)


def compile_escalation(aggregates, telemetry_path=None, existing=None, partial=False):
    """Build per-pattern escalation ladders with telemetry-adjusted start rung."""
    existing = existing or {}
    telemetry = read_telemetry_escalation(telemetry_path) if telemetry_path else {}
    base_rates = (aggregates or {}).get("base_rates") or {}
    escalation = {}

    for pattern, ladder in DEFAULT_ESCALATION.items():
        merged = list(ladder)
        prior = (existing.get("escalation") or {}).get(pattern)
        if isinstance(prior, list) and len(prior) >= len(merged):
            # Prompt-path blocks were retired in favor of transforms; migrate
            # legacy channels so old ladders don't pin the old behavior.
            merged = [LEGACY_CHANNELS.get(c, c) for c in prior]

        stats = telemetry.get(pattern) or {}
        fires = stats.get("fires", 0)
        overrides = stats.get("overrides", 0)
        start_rung = 0
        if fires >= 5 and overrides / max(fires, 1) >= 0.4:
            start_rung = min(1, len(merged) - 1)
        if fires >= 10 and overrides / max(fires, 1) >= 0.6:
            start_rung = min(2, len(merged) - 1)

        rate = base_rates.get(pattern)
        if not partial and rate is not None and rate >= 0.20 and start_rung == 0:
            start_rung = 1

        escalation[pattern] = {
            "ladder": merged,
            "start_rung": start_rung,
            "telemetry": stats,
        }
    return escalation


def _merge_template_signatures(new_items, existing_items):
    by_sig = {t["signature"]: dict(t) for t in (existing_items or [])}
    for item in new_items or []:
        sig = item.get("signature")
        if not sig:
            continue
        prior = by_sig.get(sig)
        if prior:
            prior["count"] = max(prior.get("count", 0), item.get("count", 0))
            if len(item.get("message", "")) > len(prior.get("message", "")):
                prior["message"] = item["message"]
        else:
            by_sig[sig] = dict(item)
    return list(by_sig.values())


def _merge_correction_clusters(new_items, existing_items):
    by_key = {}
    for item in existing_items or []:
        key = tuple(sorted(item.get("keywords") or []))
        if key:
            by_key[key] = dict(item)
    for item in new_items or []:
        key = tuple(sorted(item.get("keywords") or []))
        if not key:
            continue
        prior = by_key.get(key)
        if prior:
            prior["sessions"] = max(prior.get("sessions", 0), item.get("sessions", 0))
            if len(item.get("constraint_hint", "")) > len(prior.get("constraint_hint", "")):
                prior["constraint_hint"] = item["constraint_hint"]
        else:
            by_key[key] = dict(item)
    return list(by_key.values())


def _rule(rule_id, pattern, conditions, channel, message, priority,
          evidence_summary, cooldown_prompts=10, max_per_session=1):
    return {
        "id": rule_id,
        "pattern": pattern,
        "class": "C",
        "trigger": {"all": conditions},
        "action": {"channel": channel, "message": message[:400]},
        "cooldown_prompts": cooldown_prompts,
        "max_per_session": max_per_session,
        "priority": priority,
        "rights": {"blocking": False},
        "user_status": "candidate",
        "evidence_summary": evidence_summary,
    }


def compile_class_c_rules(aggregates):
    """Class-C rules only from findings with doc-specified support."""
    rules = []

    templates = [t for t in aggregates.get("template_signatures", [])
                 if t["count"] >= TEMPLATE_MIN_SUPPORT]
    if templates:
        total = sum(t["count"] for t in templates)
        rules.append(_rule(
            "c_batch_template", "batch_pipeline_in_chat",
            [{"signal": "prompt_matches_template", "op": ">=", "value": 1}],
            "stdout",
            f"This prompt matches a template you've run {total} times "
            "interactively. Run it headless (claude -p) with a fixed minimal "
            "context instead.",
            "waste",
            f"{len(templates)} template signature(s), {total} total prompts",
        ))

    handoff = aggregates.get("handoff_stats", {})
    marker_count = handoff.get("handoff_marker_prompt_count", 0)
    if marker_count >= HANDOFF_MIN_SUPPORT:
        rules.append(_rule(
            "c_handoff_ritual", "manual_handoff_ritual",
            [{"signal": "prompt_matches_handoff", "op": ">=", "value": 1}],
            "stdout",
            f"You've written handoff briefs manually {marker_count} times. "
            "Codify the ritual as a /handover command that also updates "
            "project memory.",
            "quality",
            f"{marker_count} manual handoff prompts",
        ))

    clusters = [c for c in aggregates.get("correction_clusters", [])
                if c["session_count"] >= CLUSTER_MIN_SESSIONS]
    if clusters:
        rules.append(_rule(
            "c_correction_cluster", "missing_persistent_project_memory",
            [{"signal": "prompt_matches_correction_cluster", "op": ">=", "value": 1}],
            "inject",
            "This correction has recurred across sessions. Apply the known "
            "constraint, and suggest persisting it to CLAUDE.md.",
            "quality",
            f"{len(clusters)} correction cluster(s) spanning >= "
            f"{CLUSTER_MIN_SESSIONS} sessions each",
        ))

    return rules


def _merge_patterns(new_rules, existing_patterns):
    """Recompiles must not clobber the user's promote/reject decisions."""
    status_by_id = {p.get("id"): p.get("user_status")
                    for p in existing_patterns if p.get("id")}
    merged = []
    for rule in new_rules:
        prior = status_by_id.get(rule["id"])
        if prior in ("confirmed", "rejected", "muted"):
            rule = dict(rule, user_status=prior)
        merged.append(rule)
    # Keep entries the compiler didn't regenerate (e.g. label.py-mined ones).
    new_ids = {rule["id"] for rule in merged}
    for pattern in existing_patterns:
        if pattern.get("id") and pattern["id"] not in new_ids:
            merged.append(pattern)
    return merged


def compile_rulebook(aggregates, percentiles, existing_rulebook=None,
                     telemetry_path=None, since_days=0):
    """Pure: returns the rulebook dict; the caller writes it to disk."""
    existing_rulebook = existing_rulebook or {}
    partial = since_days > 0
    new_rules = []
    for rule in compile_class_c_rules(aggregates):
        error = validate_rule(rule)
        if error:
            raise ValueError(f"compiled invalid rule {rule.get('id')}: {error}")
        new_rules.append(rule)

    handoff = aggregates.get("handoff_stats", {})
    marker_count = handoff.get("handoff_marker_prompt_count", 0)
    esc_full = compile_escalation(
        aggregates, telemetry_path=telemetry_path,
        existing=existing_rulebook.get("escalation_meta"), partial=partial)
    # Flat ladder for judge rung_for(); start_rung seeds session marks on first fire.
    escalation = {k: v["ladder"] for k, v in esc_full.items()}
    escalation_meta = esc_full

    template_signatures = [
        {
            "signature": t["signature"],
            "count": t["count"],
            "message": (f"This template has run {t['count']} times "
                        "interactively — run it headless (claude -p) "
                        "with a fixed minimal context."),
        }
        for t in aggregates.get("template_signatures", [])
        if t["count"] >= TEMPLATE_MIN_SUPPORT
    ]
    correction_clusters = [
        {
            "keywords": c["keywords"],
            "constraint_hint": ("Recurring correction — keywords: "
                                + ", ".join(c["keywords"][:6])),
            "sessions": c["session_count"],
        }
        for c in aggregates.get("correction_clusters", [])
        if c["session_count"] >= CLUSTER_MIN_SESSIONS
    ]
    handoff_block = {
        "active": marker_count >= HANDOFF_MIN_SUPPORT,
        "message": (f"You write handoff briefs manually ({marker_count} "
                    "times) — codify as /handover and update memory in "
                    "the same pass."),
    }
    if partial:
        template_signatures = _merge_template_signatures(
            template_signatures, existing_rulebook.get("template_signatures"))
        correction_clusters = _merge_correction_clusters(
            correction_clusters, existing_rulebook.get("correction_clusters"))
        if not marker_count:
            prior_handoff = existing_rulebook.get("handoff") or {}
            if prior_handoff.get("active"):
                handoff_block = dict(prior_handoff)

    return {
        "version": 2,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "thresholds": compile_thresholds(percentiles),
        "escalation": escalation,
        "escalation_meta": escalation_meta,
        "patterns": _merge_patterns(
            new_rules, existing_rulebook.get("patterns", [])),
        "template_signatures": template_signatures,
        "correction_clusters": correction_clusters,
        "handoff": handoff_block,
    }

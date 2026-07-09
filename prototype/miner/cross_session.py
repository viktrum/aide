"""Cross-session aggregation and per-user calibration.

Pure functions over lists of (SessionFeatures, metadata) pairs collected by
mine.py. No I/O here — mine.py owns files. Findings emitted here use the
same dict shape as generic_detectors._finding() plus a `sessions` list with
per-session contributors so downstream reports (and the interactive
dashboard) can drill into every aggregate claim.

Patterns covered (the AIDE design notes):
  6.57 batch_pipeline_in_chat        (template signatures, class C)
  6.55 manual_handoff_ritual         (handoff stats, class C)
  6.12 missing_persistent_project_memory (correction clusters, class C)
  6.62 premature_session_abandonment (abandonment chains, T1)
  6.60 rate_limit_context_loss       (rate-limit restarts, T1)
"""
import re
from collections import Counter
from datetime import datetime

from generic_detectors import CORRECTION_RE

TEMPLATE_MIN_COUNT = 10
TEMPLATE_MIN_WORDS = 10
HANDOFF_MIN_MARKERS = 5
HANDOFF_MAX_SLASH_RATIO = 0.5
CLUSTER_SIMILARITY = 0.5
CLUSTER_MIN_SESSIONS = 3
ABANDON_MAX_PROMPTS = 3
ABANDON_SIMILARITY = 0.6
ABANDON_WINDOW_HOURS = 24
RATE_LIMIT_WINDOW_HOURS = 6
RATE_LIMIT_MIN_RESTART_WORDS = 50
EXCERPT_LEN = 200

_WORD_RE = re.compile(r"[a-z0-9']+")


def _word_set(text):
    return set(_WORD_RE.findall((text or "").lower()))


def _similarity(a, b):
    """Jaccard similarity over word sets (mirrors prompt_judge.similarity)."""
    sa, sb = _word_set(a), _word_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_between(earlier, later):
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 3600


def _excerpt(text):
    return re.sub(r"\s+", " ", (text or "")).strip()[:EXCERPT_LEN]


def _finding(pattern, score, evidence, attribution, confidence, alert_level,
             suggested_intervention, intervention_class, tier, sessions):
    return {
        "pattern": pattern,
        "score": round(min(max(score, 0.0), 1.0), 3),
        "evidence": evidence,
        "attribution": attribution,
        "confidence": confidence,
        "alert_level": alert_level,
        "suggested_intervention": suggested_intervention,
        "intervention_class": intervention_class,
        "tier": tier,
        "sessions": sessions,
    }


def _session_ref(features, metadata, **extra):
    ref = {
        "session_id": features.session_id,
        "project": metadata.get("project", ""),
        "started_at": metadata.get("started_at", ""),
    }
    ref.update(extra)
    return ref


def _real_sessions(sessions):
    return [
        (features, metadata) for features, metadata in sessions
        if not (features.is_subagent or metadata.get("is_subagent_session"))
    ]


# ------------------------------------------------------------- aggregations

def template_signature_stats(sessions):
    """Prefix signatures repeated across the corpus (R42, feeds 6.57)."""
    counts = Counter()
    examples = {}
    contributors = {}
    for features, metadata in sessions:
        for prompt in features.user_prompts:
            if prompt.word_count < TEMPLATE_MIN_WORDS or not prompt.prefix_signature:
                continue
            signature = prompt.prefix_signature
            counts[signature] += 1
            examples.setdefault(signature, _excerpt(prompt.text))
            contributors.setdefault(signature, []).append(
                _session_ref(features, metadata, turn=prompt.turn_index))
    return [
        {
            "signature": signature,
            "count": count,
            "example": examples[signature],
            "sessions": contributors[signature],
        }
        for signature, count in counts.most_common()
        if count >= TEMPLATE_MIN_COUNT
    ]


def handoff_stats(sessions):
    marker_prompts = []
    handover_slash_uses = 0
    for features, metadata in sessions:
        handover_slash_uses += features.slash_commands.count("/handover")
        for prompt in features.user_prompts:
            if prompt.has_handoff_marker:
                marker_prompts.append(
                    _session_ref(features, metadata,
                                 turn=prompt.turn_index,
                                 example=_excerpt(prompt.text)))
    marker_count = len(marker_prompts)
    total = marker_count + handover_slash_uses
    slash_ratio = handover_slash_uses / total if total else 0.0
    return {
        "handoff_marker_prompt_count": marker_count,
        "handover_slash_uses": handover_slash_uses,
        "slash_ratio": round(slash_ratio, 3),
        "prompts": marker_prompts,
    }


def correction_clusters(sessions):
    """Recurring corrections across sessions = constraints that never made it
    into persistent memory (6.12). Greedy single-link bucketing by Jaccard."""
    corrections = []
    for features, metadata in sessions:
        for prompt in features.user_prompts:
            if CORRECTION_RE.search(prompt.text_norm):
                corrections.append({
                    "words": _word_set(prompt.text_norm),
                    "session": _session_ref(features, metadata,
                                            turn=prompt.turn_index,
                                            example=_excerpt(prompt.text)),
                })

    clusters = []
    for correction in corrections:
        for cluster in clusters:
            rep = cluster["words"]
            union = correction["words"] | rep
            if union and len(correction["words"] & rep) / len(union) >= CLUSTER_SIMILARITY:
                cluster["items"].append(correction)
                break
        else:
            clusters.append({"words": correction["words"], "items": [correction]})

    result = []
    for cluster in clusters:
        session_ids = {item["session"]["session_id"] for item in cluster["items"]}
        if len(session_ids) < CLUSTER_MIN_SESSIONS:
            continue
        shared = set.intersection(*(item["words"] for item in cluster["items"]))
        result.append({
            "keywords": sorted(shared)[:12],
            "session_count": len(session_ids),
            "prompt_count": len(cluster["items"]),
            "sessions": [item["session"] for item in cluster["items"]],
        })
    return sorted(result, key=lambda c: -c["session_count"])


def abandonment_chains(sessions):
    """Short error-terminated sessions whose task restarts in a sibling
    session within 24h (6.62, T1)."""
    candidates = []
    for features, metadata in _real_sessions(sessions):
        if not features.user_prompts:
            continue
        has_tool_error = any(event.is_error for event in features.tool_events)
        candidates.append({
            "features": features,
            "metadata": metadata,
            "abandoned": (features.user_prompt_count <= ABANDON_MAX_PROMPTS
                          and has_tool_error),
            "first_prompt": features.user_prompts[0].text,
            "ts": _parse_ts(features.first_timestamp),
        })

    chains = []
    for cand in candidates:
        if not cand["abandoned"]:
            continue
        for other in candidates:
            if other["features"].session_id == cand["features"].session_id:
                continue
            if other["metadata"].get("project") != cand["metadata"].get("project"):
                continue
            gap = _hours_between(cand["ts"], other["ts"])
            if gap is None or not (0 <= gap <= ABANDON_WINDOW_HOURS):
                continue
            sim = _similarity(cand["first_prompt"], other["first_prompt"])
            if sim < ABANDON_SIMILARITY:
                continue
            chains.append({
                "abandoned": _session_ref(cand["features"], cand["metadata"],
                                          example=_excerpt(cand["first_prompt"])),
                "restart": _session_ref(other["features"], other["metadata"],
                                        example=_excerpt(other["first_prompt"])),
                "similarity": round(sim, 3),
                "gap_hours": round(gap, 2),
            })
            break
    return chains


def rate_limit_restarts(sessions):
    """Rate-limited sessions followed by a long re-priming prompt in a fresh
    session (6.60, T1)."""
    ordered = []
    for features, metadata in _real_sessions(sessions):
        ordered.append({
            "features": features,
            "metadata": metadata,
            "ts": _parse_ts(features.first_timestamp),
        })

    restarts = []
    for cand in ordered:
        if not cand["features"].rate_limit_marker_seen:
            continue
        end_ts = _parse_ts(cand["features"].last_timestamp)
        for other in ordered:
            if other["features"].session_id == cand["features"].session_id:
                continue
            if other["metadata"].get("project") != cand["metadata"].get("project"):
                continue
            if not other["features"].user_prompts:
                continue
            gap = _hours_between(end_ts, other["ts"])
            if gap is None or not (0 <= gap <= RATE_LIMIT_WINDOW_HOURS):
                continue
            first_prompt = other["features"].user_prompts[0]
            if first_prompt.word_count <= RATE_LIMIT_MIN_RESTART_WORDS:
                continue
            restarts.append({
                "limited": _session_ref(cand["features"], cand["metadata"]),
                "restart": _session_ref(other["features"], other["metadata"],
                                        example=_excerpt(first_prompt.text)),
                "restart_prompt_words": first_prompt.word_count,
                "gap_hours": round(gap, 2),
            })
            break
    return restarts


# ------------------------------------------------------------------ entry

def aggregate(sessions):
    """Cross-session stats + findings. `sessions` is a list of
    (SessionFeatures, metadata) pairs; metadata comes from
    mine.session_metadata()."""
    templates = template_signature_stats(sessions)
    handoff = handoff_stats(sessions)
    clusters = correction_clusters(sessions)
    chains = abandonment_chains(sessions)
    restarts = rate_limit_restarts(sessions)

    findings = []
    for template in templates:
        findings.append(_finding(
            "batch_pipeline_in_chat",
            0.6 + min(template["count"], 100) / 250,
            [
                f"template_signature={template['signature']}",
                f"count={template['count']}",
                f"example_prompt={template['example']}",
            ],
            "developer", "high", "passive_insight",
            f"This prompt template has run {template['count']} times "
            "interactively. Run it headless (claude -p) or through a subagent "
            "with a fixed minimal context instead of re-paying the session "
            "context each time.",
            "C", "T0",
            template["sessions"],
        ))

    if (handoff["handoff_marker_prompt_count"] >= HANDOFF_MIN_MARKERS
            and handoff["slash_ratio"] < HANDOFF_MAX_SLASH_RATIO):
        findings.append(_finding(
            "manual_handoff_ritual",
            0.5 + min(handoff["handoff_marker_prompt_count"], 50) / 100,
            [
                f"handoff_marker_prompt_count={handoff['handoff_marker_prompt_count']}",
                f"handover_slash_uses={handoff['handover_slash_uses']}",
                f"slash_ratio={handoff['slash_ratio']}",
            ],
            "developer", "high", "passive_insight",
            "You write session handoff briefs manually. Codify the ritual as "
            "a /handover command with a fixed template that also updates "
            "project memory in the same pass.",
            "C", "T0",
            handoff["prompts"],
        ))

    for cluster in clusters:
        findings.append(_finding(
            "missing_persistent_project_memory",
            0.5 + min(cluster["session_count"], 10) / 20,
            [
                f"cluster_keywords={','.join(cluster['keywords'])}",
                f"session_count={cluster['session_count']}",
                f"prompt_count={cluster['prompt_count']}",
            ],
            "developer", "medium", "passive_insight",
            "The same correction recurs across sessions. Persist the "
            "constraint in CLAUDE.md or project memory so each new session "
            "starts with it.",
            "C", "T0",
            cluster["sessions"],
        ))

    for chain in chains:
        findings.append(_finding(
            "premature_session_abandonment",
            0.4 + chain["similarity"] / 4,
            [
                f"abandoned_session={chain['abandoned']['session_id']}",
                f"restart_session={chain['restart']['session_id']}",
                f"first_prompt_similarity={chain['similarity']}",
                f"gap_hours={chain['gap_hours']}",
            ],
            "developer", "medium", "log_only",
            "A short error-terminated session was restarted from scratch. "
            "Fixing the blocking error in place usually costs less than "
            "re-priming a new session.",
            "C", "T1",
            [chain["abandoned"], chain["restart"]],
        ))

    for restart in restarts:
        findings.append(_finding(
            "rate_limit_context_loss",
            0.5,
            [
                f"limited_session={restart['limited']['session_id']}",
                f"restart_session={restart['restart']['session_id']}",
                f"restart_prompt_words={restart['restart_prompt_words']}",
                f"gap_hours={restart['gap_hours']}",
            ],
            "tool_or_platform", "medium", "log_only",
            "A rate-limited session was re-primed by hand. Keep a running "
            "handoff brief (or /handover) so limit interruptions resume "
            "cheaply.",
            "C", "T1",
            [restart["limited"], restart["restart"]],
        ))

    return {
        "template_signatures": templates,
        "handoff_stats": handoff,
        "correction_clusters": clusters,
        "abandonment_chains": chains,
        "rate_limit_restarts": restarts,
        "findings": findings,
    }


# -------------------------------------------------------------- calibration

def _percentiles(values):
    if not values:
        return {"p50": 0, "p90": 0, "p95": 0}
    ordered = sorted(values)
    def pick(q):
        return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]
    return {"p50": pick(0.50), "p90": pick(0.90), "p95": pick(0.95)}


def calibrate(sessions):
    """Per-user percentile baselines, excluding subagent sessions. Written to
    baseline.json under "percentiles" and consumed by rulebook_compiler."""
    real = _real_sessions(sessions)
    wall_clock = [f.wall_clock_minutes for f, _ in real if f.wall_clock_minutes > 0]
    prompt_counts = [f.user_prompt_count for f, _ in real if f.user_prompt_count > 0]
    cache_peaks = [max(f.cache_read_series) for f, _ in real if f.cache_read_series]
    word_counts = [p.word_count for f, _ in real for p in f.user_prompts]
    interrupt_rates = [
        100.0 * f.interrupt_count / f.user_prompt_count
        for f, _ in real if f.user_prompt_count > 0
    ]
    return {
        "session_wall_clock_minutes": _percentiles([round(v, 1) for v in wall_clock]),
        "user_prompt_count": _percentiles(prompt_counts),
        "cache_read_peak": _percentiles(cache_peaks),
        "prompt_word_count": _percentiles(word_counts),
        "interrupt_rate_per_100_prompts": _percentiles(
            [round(v, 2) for v in interrupt_rates]),
        "sessions_used": len(real),
    }

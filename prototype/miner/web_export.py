"""Presentation-agnostic export for a future interactive dashboard.

Produces dashboard_data.json: structured facts the UI can render however it
wants. No HTML, no chart assumptions — just sessions, findings, aggregates,
and drill-down anchors with enough context to explain each claim.

The dashboard design is not fixed; this module only guarantees the data layer.
"""
import json
import re
from collections import Counter, defaultdict
from datetime import datetime


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _day_key(ts):
    parsed = _parse_ts(ts)
    return parsed.strftime("%Y-%m-%d") if parsed else "unknown"


def _trim(text, limit=300):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _session_record(features, metadata, findings):
    prompts = [
        {
            "turn": p.turn_index,
            "entry_index": p.entry_index,
            "timestamp": p.timestamp,
            "text_excerpt": _trim(p.text, 400),
            "word_count": p.word_count,
            "question_mark_count": p.question_mark_count,
            "has_handoff_marker": p.has_handoff_marker,
            "prefix_signature": p.prefix_signature,
            "is_vague": p.is_vague,
            "is_status_ack": p.is_status_ack,
        }
        for p in features.user_prompts
    ]
    return {
        "session_id": features.session_id,
        "path": features.path,
        "project": metadata.get("project", ""),
        "git_branch": metadata.get("git_branch", ""),
        "started_at": metadata.get("started_at", ""),
        "ended_at": metadata.get("ended_at", ""),
        "is_subagent": features.is_subagent or metadata.get("is_subagent_session"),
        "metrics": {
            "user_prompt_count": features.user_prompt_count,
            "wall_clock_minutes": round(features.wall_clock_minutes, 1),
            "total_tokens": features.total_tokens,
            "max_context_tokens": features.max_context_tokens,
            "context_pct": round(features.context_pct, 3),
            "interrupt_count": features.interrupt_count,
            "compaction_continuation_count": features.compaction_continuation_count,
            "away_summary_count": features.away_summary_count,
            "file_edit_count": features.file_edit_count,
            "verification_command_count": features.verification_command_count,
            "cache_read_peak": max(features.cache_read_series, default=0),
            "slash_commands": features.slash_commands,
            "model_ids": features.model_ids,
        },
        "prompts": prompts,
        "finding_ids": [f.get("id") for f in findings if f.get("id")],
        "patterns": sorted({f["pattern"] for f in findings}),
    }


def _finding_record(finding, index):
    anchor = {}
    sessions = finding.get("sessions") or []
    if sessions:
        anchor = sessions[0]
    elif finding.get("session_id"):
        anchor = {
            "session_id": finding.get("session_id"),
            "project": finding.get("project", ""),
            "started_at": finding.get("started_at", ""),
        }
    return {
        "id": f"f-{index:04d}",
        "pattern": finding.get("pattern"),
        "score": finding.get("score"),
        "confidence": finding.get("confidence"),
        "alert_level": finding.get("alert_level"),
        "attribution": finding.get("attribution"),
        "intervention_class": finding.get("intervention_class"),
        "tier": finding.get("tier"),
        "evidence": finding.get("evidence", []),
        "suggested_intervention": finding.get("suggested_intervention"),
        "anchor": anchor,
        "contributors": sessions,
        "session_id": finding.get("session_id"),
        "session_path": finding.get("session_path"),
        "project": finding.get("project"),
        "repo": finding.get("repo"),
    }


def _timeline_buckets(sessions):
    by_day = defaultdict(lambda: {
        "sessions": 0, "prompts": 0, "findings": 0, "tokens": 0,
    })
    for record in sessions:
        day = _day_key(record.get("started_at"))
        by_day[day]["sessions"] += 1
        by_day[day]["prompts"] += record["metrics"]["user_prompt_count"]
        by_day[day]["tokens"] += record["metrics"]["total_tokens"]
    return dict(sorted(by_day.items()))


def build_dashboard_export(
    *,
    generated_at,
    detector_version,
    session_pairs,
    session_findings,
    cross_session_findings,
    config_findings,
    aggregates,
    percentiles,
    baseline,
    escalation_meta=None,
):
    """Build the full dashboard payload from miner outputs."""
    all_findings = []
    indexed = []
    counter = 0

    for findings in session_findings.values():
        for finding in findings:
            counter += 1
            record = _finding_record(finding, counter)
            finding = dict(finding, id=record["id"])
            indexed.append(record)
            all_findings.append(finding)

    for finding in cross_session_findings + config_findings:
        counter += 1
        record = _finding_record(finding, counter)
        finding = dict(finding, id=record["id"])
        indexed.append(record)
        all_findings.append(finding)

    sessions = []
    for features, metadata in session_pairs:
        sid = features.session_id
        sessions.append(_session_record(
            features, metadata, session_findings.get(sid, [])))

    pattern_counts = Counter(f["pattern"] for f in all_findings)
    class_counts = Counter(f.get("intervention_class", "unknown") for f in all_findings)
    attribution_counts = Counter(f.get("attribution", "unknown") for f in all_findings)

    timeline = _timeline_buckets(sessions)
    for finding in all_findings:
        day = _day_key(finding.get("started_at") or "")
        if day in timeline:
            timeline[day]["findings"] += 1

    ranked = sorted(
        [{"pattern": p, "count": c,
          "avg_score": round(
              sum(f["score"] for f in all_findings if f["pattern"] == p) / c, 3)}
         for p, c in pattern_counts.items()],
        key=lambda item: -(item["count"] * item["avg_score"]),
    )

    dates = [s.get("started_at") for s in sessions if s.get("started_at")]
    return {
        "meta": {
            "generated_at": generated_at,
            "detector_version": detector_version,
            "sessions_scanned": len(sessions),
            "findings_total": len(all_findings),
            "date_range": {
                "start": min(dates) if dates else "",
                "end": max(dates) if dates else "",
            },
        },
        "summary": {
            "by_pattern": dict(pattern_counts),
            "by_intervention_class": dict(class_counts),
            "by_attribution": dict(attribution_counts),
            "ranked_patterns": ranked[:20],
            "segments_flagged": baseline.get("segments_flagged", 0),
            "agent_recovered_streaks": baseline.get("agent_recovered_streaks", 0),
        },
        "percentiles": percentiles,
        "timeline": timeline,
        "sessions": sessions,
        "findings": indexed,
        "cross_session": {
            "template_signatures": aggregates.get("template_signatures", []),
            "handoff_stats": aggregates.get("handoff_stats", {}),
            "correction_clusters": aggregates.get("correction_clusters", []),
            "abandonment_chains": aggregates.get("abandonment_chains", []),
            "rate_limit_restarts": aggregates.get("rate_limit_restarts", []),
            "escalation": escalation_meta or {},
        },
        "config_lints": [
            {
                "pattern": f.get("pattern"),
                "repo": f.get("repo"),
                "confidence": f.get("confidence"),
                "evidence": f.get("evidence", []),
                "suggested_patch": f.get("suggested_patch", ""),
            }
            for f in config_findings
        ],
    }


def write_dashboard_export(path, payload):
    Path = __import__("pathlib").Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))

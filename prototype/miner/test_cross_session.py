import re
import unittest

from cross_session import aggregate, calibrate
from session_features import (SessionFeatures, ToolEventFeature,
                              UserPromptFeature, normalize_for_detection)


def prompt(text, *, turn_index=0, has_handoff_marker=False, timestamp=""):
    text_norm = normalize_for_detection(text)
    return UserPromptFeature(
        entry_index=turn_index,
        turn_index=turn_index,
        text=text,
        text_norm=text_norm,
        word_count=len(re.findall(r"[a-z0-9']+", text_norm)),
        imperative_count=1,
        has_file_ref=False,
        is_vague=False,
        has_plan_marker=False,
        has_verify_marker=False,
        has_review_marker=False,
        has_clear_marker=False,
        has_typo_marker=False,
        is_status_ack=False,
        question_mark_count=text.count("?"),
        has_handoff_marker=has_handoff_marker,
        prefix_signature=re.sub(r"\d+", "#", text[:30]),
        timestamp=timestamp,
    )


def tool_error():
    return ToolEventFeature(
        entry_index=0, name="Bash", input_text="", output_text="boom",
        is_error=True, is_read=False, is_edit=False, is_verification=False,
        output_chars=4)


def session(session_id, prompts, *, project="/proj/a", first_ts="", last_ts="",
            is_subagent=False, tool_events=None, slash_commands=None,
            rate_limit=False, interrupt_count=0, cache_read_series=None):
    features = SessionFeatures(
        session_id=session_id,
        path=f"{session_id}.jsonl",
        user_prompts=prompts,
    )
    features.first_timestamp = first_ts
    features.last_timestamp = last_ts or first_ts
    features.is_subagent = is_subagent
    features.tool_events = tool_events or []
    features.slash_commands = slash_commands or []
    features.rate_limit_marker_seen = rate_limit
    features.interrupt_count = interrupt_count
    features.cache_read_series = cache_read_series or []
    metadata = {
        "project": project,
        "started_at": first_ts,
        "ended_at": last_ts or first_ts,
        "is_subagent_session": is_subagent,
    }
    return features, metadata


def patterns(result):
    return {finding["pattern"] for finding in result["findings"]}


class TemplateSignatureTests(unittest.TestCase):
    def test_twelve_repeats_fire_batch_pipeline(self):
        text = "CSV ROW 5 (the event to identify and classify for the pipeline)"
        sessions = [
            session(f"s{i}", [prompt(text.replace("5", str(i % 10)))])
            for i in range(12)
        ]

        result = aggregate(sessions)

        self.assertIn("batch_pipeline_in_chat", patterns(result))
        template = result["template_signatures"][0]
        self.assertEqual(template["count"], 12)
        self.assertEqual(len(template["sessions"]), 12)
        self.assertTrue(template["example"])

    def test_nine_repeats_do_not_fire(self):
        text = "CSV ROW 5 (the event to identify and classify for the pipeline)"
        sessions = [session(f"s{i}", [prompt(text)]) for i in range(9)]

        result = aggregate(sessions)

        self.assertNotIn("batch_pipeline_in_chat", patterns(result))

    def test_short_prompts_are_ignored(self):
        sessions = [session(f"s{i}", [prompt("run batch 42 now")])
                    for i in range(15)]

        result = aggregate(sessions)

        self.assertEqual(result["template_signatures"], [])


class HandoffTests(unittest.TestCase):
    def test_six_markers_no_slash_fires(self):
        sessions = [
            session(f"s{i}", [prompt(
                "please prepare for next session handoff with what's done what's left",
                has_handoff_marker=True)])
            for i in range(6)
        ]

        result = aggregate(sessions)

        self.assertIn("manual_handoff_ritual", patterns(result))
        self.assertEqual(result["handoff_stats"]["handoff_marker_prompt_count"], 6)

    def test_markers_with_dominant_slash_usage_do_not_fire(self):
        sessions = [
            session(f"s{i}", [prompt("session handoff notes",
                                     has_handoff_marker=True)],
                    slash_commands=["/handover", "/handover"])
            for i in range(6)
        ]

        result = aggregate(sessions)

        self.assertNotIn("manual_handoff_ritual", patterns(result))


class CorrectionClusterTests(unittest.TestCase):
    CORRECTION = "that's wrong, use the forked spine revert flow for billing exports"

    def test_corrections_across_three_sessions_cluster(self):
        sessions = [session(f"s{i}", [prompt(self.CORRECTION)]) for i in range(3)]

        result = aggregate(sessions)

        self.assertIn("missing_persistent_project_memory", patterns(result))
        cluster = result["correction_clusters"][0]
        self.assertEqual(cluster["session_count"], 3)
        self.assertTrue(cluster["keywords"])

    def test_corrections_in_one_session_do_not_cluster(self):
        sessions = [session("s0", [
            prompt(self.CORRECTION, turn_index=0),
            prompt(self.CORRECTION, turn_index=1),
            prompt(self.CORRECTION, turn_index=2),
        ])]

        result = aggregate(sessions)

        self.assertNotIn("missing_persistent_project_memory", patterns(result))


class AbandonmentTests(unittest.TestCase):
    FIRST = "migrate the billing exporter to the new warehouse schema with tests"

    def test_error_abandonment_with_similar_restart_fires(self):
        sessions = [
            session("dead", [prompt(self.FIRST)],
                    first_ts="2026-07-01T10:00:00Z",
                    tool_events=[tool_error()]),
            session("retry", [prompt(self.FIRST + " again please")],
                    first_ts="2026-07-01T14:00:00Z"),
        ]

        result = aggregate(sessions)

        self.assertIn("premature_session_abandonment", patterns(result))
        chain = result["abandonment_chains"][0]
        self.assertEqual(chain["abandoned"]["session_id"], "dead")
        self.assertEqual(chain["restart"]["session_id"], "retry")

    def test_no_tool_error_means_no_chain(self):
        sessions = [
            session("dead", [prompt(self.FIRST)], first_ts="2026-07-01T10:00:00Z"),
            session("retry", [prompt(self.FIRST)], first_ts="2026-07-01T14:00:00Z"),
        ]

        result = aggregate(sessions)

        self.assertNotIn("premature_session_abandonment", patterns(result))

    def test_restart_in_other_project_does_not_chain(self):
        sessions = [
            session("dead", [prompt(self.FIRST)],
                    first_ts="2026-07-01T10:00:00Z",
                    tool_events=[tool_error()]),
            session("retry", [prompt(self.FIRST)], project="/proj/b",
                    first_ts="2026-07-01T14:00:00Z"),
        ]

        result = aggregate(sessions)

        self.assertNotIn("premature_session_abandonment", patterns(result))


class RateLimitTests(unittest.TestCase):
    LONG_RESTART = ("we were working on the billing exporter migration before the "
                    "limit hit; context: the schema lives in warehouse/models.py, "
                    "constraints are no breaking changes to v1 consumers, tests in "
                    "tests/test_exporter.py must pass, and the docs table mapping "
                    "needs regeneration after any column rename or type change "
                    "so start by reading both files before editing anything")

    def test_rate_limited_session_with_long_restart_fires(self):
        sessions = [
            session("limited", [prompt("keep going on the exporter migration")],
                    first_ts="2026-07-01T10:00:00Z",
                    last_ts="2026-07-01T11:00:00Z", rate_limit=True),
            session("restart", [prompt(self.LONG_RESTART)],
                    first_ts="2026-07-01T13:00:00Z"),
        ]

        result = aggregate(sessions)

        self.assertIn("rate_limit_context_loss", patterns(result))

    def test_short_restart_prompt_does_not_fire(self):
        sessions = [
            session("limited", [prompt("keep going")],
                    first_ts="2026-07-01T10:00:00Z",
                    last_ts="2026-07-01T11:00:00Z", rate_limit=True),
            session("restart", [prompt("continue the exporter work")],
                    first_ts="2026-07-01T13:00:00Z"),
        ]

        result = aggregate(sessions)

        self.assertNotIn("rate_limit_context_loss", patterns(result))


class CalibrationTests(unittest.TestCase):
    def test_percentiles_exclude_subagents(self):
        sessions = [
            session("s0", [prompt("do the migration work now please today")],
                    first_ts="2026-07-01T10:00:00Z",
                    last_ts="2026-07-01T10:30:00Z",
                    cache_read_series=[10_000], interrupt_count=1),
            session("agent-x", [prompt("subagent work item")],
                    first_ts="2026-07-01T10:00:00Z",
                    last_ts="2026-07-02T10:00:00Z",
                    is_subagent=True, cache_read_series=[900_000]),
        ]

        result = calibrate(sessions)

        self.assertEqual(result["sessions_used"], 1)
        self.assertEqual(result["session_wall_clock_minutes"]["p95"], 30.0)
        self.assertEqual(result["cache_read_peak"]["p95"], 10_000)
        self.assertEqual(result["interrupt_rate_per_100_prompts"]["p50"], 100.0)

    def test_empty_corpus_returns_zeroed_percentiles(self):
        result = calibrate([])

        self.assertEqual(result["user_prompt_count"], {"p50": 0, "p90": 0, "p95": 0})


class FindingShapeTests(unittest.TestCase):
    def test_aggregate_findings_have_required_keys(self):
        text = "CSV ROW 5 (the event to identify and classify for the pipeline)"
        sessions = [session(f"s{i}", [prompt(text)]) for i in range(12)]

        result = aggregate(sessions)

        required = {"pattern", "score", "evidence", "attribution", "confidence",
                    "alert_level", "suggested_intervention",
                    "intervention_class", "tier", "sessions"}
        for finding in result["findings"]:
            self.assertEqual(set(finding), required)
            self.assertIn(finding["intervention_class"], {"A", "B", "C", "D-report", "D-patch"})
            self.assertIn(finding["tier"], {"T0", "T1"})


if __name__ == "__main__":
    unittest.main()

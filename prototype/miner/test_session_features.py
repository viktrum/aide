import json
import tempfile
import unittest
from pathlib import Path

from session_features import (
    MalformedSessionEntry,
    build_session_features,
    normalize_for_detection,
)


def write_jsonl(entries):
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl")
    with tmp:
        for entry in entries:
            tmp.write(json.dumps(entry) + "\n")
    return Path(tmp.name)


class SessionFeatureTests(unittest.TestCase):
    def test_normalize_for_detection_collapses_repeats_and_zero_width(self):
        self.assertEqual(
            normalize_for_detection(" Fixx\u200bx   THIS\u200d "),
            "fixx this",
        )

    def test_ignores_user_tool_result_entries_as_prompts(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "Fix auth.py"}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": False, "content": "ok"},
            ]}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        ])

        features = build_session_features(path)

        self.assertEqual(len(features.user_prompts), 1)
        self.assertEqual(features.user_prompts[0].text, "Fix auth.py")

    def test_ignores_continuation_summary_as_real_prompt(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": (
                "This session is being continued from a previous conversation "
                "that ran out of context. The summary below mentions a typo."
            )}},
            {"type": "user", "message": {"content": "Fix auth.py"}},
        ])

        features = build_session_features(path)

        self.assertEqual(len(features.user_prompts), 1)
        self.assertEqual(features.user_prompts[0].text, "Fix auth.py")

    def test_non_dict_jsonl_entry_raises_malformed_session_entry(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "Fix auth.py"}},
            [],
        ])

        with self.assertRaises(MalformedSessionEntry) as raised:
            build_session_features(path)

        message = str(raised.exception)
        self.assertIn(str(path), message)
        self.assertIn(":2:", message)
        self.assertIn("expected JSON object", message)
        self.assertIn("list", message)

    def test_non_dict_message_raises_malformed_session_entry(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "Fix auth.py"}},
            {"type": "assistant", "message": [{"content": "x"}]},
        ])

        with self.assertRaises(MalformedSessionEntry) as raised:
            build_session_features(path)

        message = str(raised.exception)
        self.assertIn(str(path), message)
        self.assertIn(":2:", message)
        self.assertIn("expected message object", message)
        self.assertIn("list", message)

    def test_counts_read_and_edit_tool_use_events(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "Fix auth.py and run tests"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "id": "toolu_read", "input": {"file_path": "auth.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_read",
                 "is_error": False, "content": "file contents"},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit",
                 "id": "toolu_edit", "input": {"file_path": "auth.py",
                                                "old_string": "a",
                                                "new_string": "b"}},
            ]}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.file_read_count, 1)
        self.assertEqual(features.file_edit_count, 1)
        self.assertEqual(features.files_read_before_first_edit, 1)
        self.assertEqual(features.tool_output_chars_before_first_edit, len("file contents"))
        self.assertEqual([event.name for event in features.tool_events], ["Read", "Edit"])

    def test_counts_verification_command_in_tool_input(self):
        path = write_jsonl([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "pnpm build && npm test"}},
            ]}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.verification_command_count, 1)
        self.assertTrue(features.tool_events[0].is_verification)

    def test_counts_context_tokens_from_assistant_usage(self):
        path = write_jsonl([
            {"type": "assistant", "message": {
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 10,
                          "cache_creation_input_tokens": 5},
                "content": [],
            }},
            {"type": "assistant", "message": {
                "usage": {"input_tokens": 50, "output_tokens": 7,
                          "cache_read_input_tokens": 200,
                          "cache_creation_input_tokens": 3},
                "content": [],
            }},
        ])

        features = build_session_features(path)

        self.assertEqual(features.total_tokens, 395)
        self.assertEqual(features.max_context_tokens, 253)

    def test_status_acks_are_not_vague_prompts(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "go ahead"}},
            {"type": "user", "message": {"content": "continue"}},
            {"type": "user", "message": {"content": "status?"}},
        ])

        features = build_session_features(path)

        self.assertEqual(len(features.user_prompts), 3)
        self.assertTrue(all(prompt.is_status_ack for prompt in features.user_prompts))
        self.assertFalse(any(prompt.is_vague for prompt in features.user_prompts))

    def test_do_it_is_vague_not_status_ack(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "do it"}},
            {"type": "user", "message": {"content": "go ahead"}},
            {"type": "user", "message": {"content": "continue"}},
            {"type": "user", "message": {"content": "status?"}},
        ])

        features = build_session_features(path)
        prompts = features.user_prompts

        self.assertTrue(prompts[0].is_vague)
        self.assertFalse(prompts[0].is_status_ack)
        self.assertTrue(all(prompt.is_status_ack for prompt in prompts[1:]))
        self.assertFalse(any(prompt.is_vague for prompt in prompts[1:]))

    def test_unknown_tool_result_id_does_not_attach_to_pending_event(self):
        path = write_jsonl([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "id": "toolu_read", "input": {"file_path": "auth.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "missing_tool",
                 "is_error": False, "content": "unrelated output"},
            ]}},
        ])

        features = build_session_features(path)

        self.assertEqual(len(features.tool_events), 1)
        self.assertEqual(features.tool_events[0].tool_id, "toolu_read")
        self.assertEqual(features.tool_events[0].output_text, "")
        self.assertEqual(features.tool_events[0].output_chars, 0)

    def test_tool_results_attach_by_id_when_out_of_order(self):
        path = write_jsonl([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read",
                 "id": "toolu_first", "input": {"file_path": "first.py"}},
                {"type": "tool_use", "name": "Read",
                 "id": "toolu_second", "input": {"file_path": "second.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_second",
                 "is_error": False, "content": "second output"},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_first",
                 "is_error": False, "content": "first output"},
            ]}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.tool_events[0].tool_id, "toolu_first")
        self.assertEqual(features.tool_events[0].output_text, "first output")
        self.assertEqual(features.tool_events[1].tool_id, "toolu_second")
        self.assertEqual(features.tool_events[1].output_text, "second output")

    def test_counts_user_prompt_markers(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "/clear"}},
            {"type": "user", "message": {"content": "plan first, then run pytest"}},
            {"type": "user", "message": {"content": "review the diff"}},
            {"type": "user", "message": {"content": "typo, I meant auth.py"}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.clear_or_compact_marker_count, 1)
        self.assertEqual(features.planning_marker_count, 1)
        self.assertEqual(features.review_marker_count, 1)
        self.assertEqual(features.typo_marker_count, 1)

    def test_counts_interrupt_markers_and_excludes_them_from_prompts(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user]"},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user for tool use]"},
            ]}},
            {"type": "user", "message": {"content": "Fix auth.py"}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.interrupt_count, 2)
        self.assertEqual(len(features.user_prompts), 1)

    def test_counts_compaction_continuation_markers(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": (
                "This session is being continued from a previous conversation "
                "that ran out of context."
            )}},
            {"type": "user", "message": {"content": "Fix auth.py"}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.compaction_continuation_count, 1)
        self.assertEqual(len(features.user_prompts), 1)

    def test_extracts_tagged_and_bare_slash_commands(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": (
                "<command-name>/clear</command-name>"
                "<command-message>clear</command-message>"
            )}},
            {"type": "user", "message": {"content": "/model"}},
            {"type": "user", "message": {"content": (
                "<command-name>/compact</command-name>"
            )}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.slash_commands, ["/clear", "/model", "/compact"])
        self.assertEqual(features.clear_command_count, 1)
        self.assertEqual(features.compact_command_count, 1)
        self.assertEqual(features.model_switch_count, 1)

    def test_tracks_permission_modes_and_auto_mode(self):
        path = write_jsonl([
            {"type": "permission-mode", "permissionMode": "plan"},
            {"type": "permission-mode", "permissionMode": "auto"},
        ])

        features = build_session_features(path)

        self.assertEqual(features.permission_modes, ["plan", "auto"])
        self.assertTrue(features.auto_mode_active)

    def test_counts_away_summaries_and_api_errors(self):
        path = write_jsonl([
            {"type": "system", "subtype": "away_summary", "content": "recap"},
            {"type": "system", "subtype": "away_summary", "content": "recap 2"},
            {"type": "system", "subtype": "api_error", "content": "boom"},
        ])

        features = build_session_features(path)

        self.assertEqual(features.away_summary_count, 2)
        self.assertEqual(features.api_error_count, 1)

    def test_bash_after_error_flag_tracks_previous_tool_result(self):
        path = write_jsonl([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "id": "t1",
                 "input": {"command": "bun run   typecheck"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "is_error": True, "content": "type error in auth.ts"},
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "id": "t2",
                 "input": {"command": "bun run typecheck"}},
            ]}},
        ])

        features = build_session_features(path)

        self.assertEqual(len(features.bash_commands), 2)
        self.assertEqual(features.bash_commands[0]["norm"], "bun run typecheck")
        self.assertFalse(features.bash_commands[0]["after_error"])
        self.assertTrue(features.bash_commands[1]["after_error"])

    def test_web_tool_runs_reset_on_user_prompt(self):
        path = write_jsonl([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "a"}},
                {"type": "tool_use", "name": "WebSearch", "input": {"query": "b"}},
                {"type": "tool_use", "name": "WebFetch", "input": {"url": "c"}},
            ]}},
            {"type": "user", "message": {"content": "now something else"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__browser__web_search",
                 "input": {"query": "d"}},
            ]}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.web_tool_runs, [3, 1])

    def test_prompt_question_marks_handoff_and_signature(self):
        path = write_jsonl([
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {
                "content": "what is x? why y? and how does 42 work?"}},
            {"type": "user", "message": {
                "content": "lets prepare for next session handoff"}},
        ])

        features = build_session_features(path)
        first, second = features.user_prompts

        self.assertEqual(first.question_mark_count, 3)
        self.assertEqual(first.timestamp, "2026-07-01T10:00:00Z")
        self.assertEqual(first.prefix_signature, "what is x? why y? and how does")
        self.assertFalse(first.has_handoff_marker)
        self.assertTrue(second.has_handoff_marker)

    def test_wall_clock_and_timestamps(self):
        path = write_jsonl([
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z",
             "message": {"content": "start"}},
            {"type": "assistant", "timestamp": "2026-07-01T11:30:00Z",
             "message": {"content": []}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.first_timestamp, "2026-07-01T10:00:00Z")
        self.assertEqual(features.last_timestamp, "2026-07-01T11:30:00Z")
        self.assertAlmostEqual(features.wall_clock_minutes, 90.0)

    def test_sidechain_marks_subagent(self):
        path = write_jsonl([
            {"type": "user", "isSidechain": True, "message": {"content": "child task"}},
        ])

        features = build_session_features(path)

        self.assertTrue(features.is_subagent)
        self.assertEqual(features.sidechain_event_count, 1)

    def test_rate_limit_marker(self):
        path = write_jsonl([
            {"type": "user", "message": {"content": "usage limit reached, resuming later"}},
        ])

        features = build_session_features(path)

        self.assertTrue(features.rate_limit_marker_seen)

    def test_cache_read_series_collected(self):
        path = write_jsonl([
            {"type": "assistant", "message": {
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000},
                "content": []}},
            {"type": "assistant", "message": {
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 90000},
                "content": []}},
        ])

        features = build_session_features(path)

        self.assertEqual(features.cache_read_series, [1000, 90000])


if __name__ == "__main__":
    unittest.main()

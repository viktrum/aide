import json
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "miner"))
from label import (
    consolidate_candidates,
    remap_candidate_indices,
    validate_candidate,
    triggers_partition_pair,
    uses_only_shape_signals,
    merge_into_rulebook,
    DEFAULT_BATCH_SIZE,
)


class LabelBatchTests(unittest.TestCase):
    def test_remap_indices(self):
        c = {"aggregation_key": "retry_x", "supporting_segments": [0, 2],
             "category": "recovery", "trigger": {"all": [{"signal": "turns", "op": ">=", "value": 1}]},
             "action": {"channel": "inject", "message": "Say what failed before retrying."}}
        out = remap_candidate_indices(c, 10)
        self.assertEqual(out["supporting_segments"], [10, 12])

    def test_consolidate_across_batches(self):
        a = {"aggregation_key": "retry_x", "supporting_segments": [1, 2],
             "category": "recovery", "description": "short",
             "trigger": {"all": [{"signal": "turns", "op": ">=", "value": 2}]},
             "action": {"channel": "inject", "message": "Msg one"}}
        b = {"aggregation_key": "retry_x", "supporting_segments": [15],
             "category": "recovery", "description": "longer description",
             "trigger": {"all": [{"signal": "turns", "op": ">=", "value": 2}]},
             "action": {"channel": "inject", "message": "Much longer message here"}}
        merged = consolidate_candidates([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["supporting_segments"], [1, 2, 15])
        self.assertEqual(merged[0]["description"], "longer description")

    def test_default_batch_size(self):
        self.assertEqual(DEFAULT_BATCH_SIZE, 10)

    def test_shape_only_trigger_rejected(self):
        c = {
            "aggregation_key": "shape_only",
            "supporting_segments": [0, 1],
            "category": "recovery",
            "trigger": {"all": [{"signal": "prompt_word_count", "op": ">=", "value": 5}]},
            "action": {"channel": "inject", "message": "This message is long enough to pass."},
        }
        err = validate_candidate(c, 5)
        self.assertIn("layout signals", err)

    def test_behavioral_retry_trigger_accepted(self):
        """Official pattern-miner example — must not be rejected."""
        c = {
            "aggregation_key": "retry_without_new_information",
            "supporting_segments": [0, 1],
            "category": "recovery",
            "trigger": {"all": [
                {"signal": "prompt_similarity_to_failed", "op": ">=", "value": 0.8},
                {"signal": "consecutive_error_turns", "op": ">=", "value": 1},
            ]},
            "action": {"channel": "inject",
                       "message": "Your last attempt failed. Say what it got wrong instead of repeating."},
        }
        self.assertIsNone(validate_candidate(c, 5))

    def test_content_trigger_accepted(self):
        c = {
            "aggregation_key": "content_ok",
            "supporting_segments": [0, 1],
            "category": "recovery",
            "trigger": {"all": [
                {"signal": "prompt_word_count", "op": ">=", "value": 5},
                {"signal": "prompt_mentions_internal_tool_names", "op": ">=", "value": 1},
            ]},
            "action": {"channel": "inject", "message": "Avoid dictating internal tool names."},
        }
        self.assertIsNone(validate_candidate(c, 5))

    def test_partition_pair_detected(self):
        a = {"trigger": {"all": [{"signal": "prompt_word_count", "op": ">=", "value": 10}]}}
        b = {"trigger": {"all": [{"signal": "prompt_word_count", "op": "<", "value": 10}]}}
        self.assertTrue(triggers_partition_pair(a, b))

    def test_idempotent_merge_occurrences(self):
        import tempfile
        segments = [
            {"session_id": "s1", "turn": 1, "excerpt": "a"},
            {"session_id": "s2", "turn": 2, "excerpt": "b"},
            {"session_id": "s3", "turn": 3, "excerpt": "c"},
        ]
        cand = [{
            "aggregation_key": "retry_x",
            "supporting_segments": [0, 1],
            "category": "recovery",
            "trigger": {"all": [{"signal": "turns", "op": ">=", "value": 2},
                               {"signal": "prompt_mentions_internal_tool_names", "op": ">=", "value": 1}]},
            "action": {"channel": "inject", "message": "Say what failed before retrying."},
        }]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rulebook.json"
            merge_into_rulebook(cand, segments, path)
            first = json.loads(path.read_text())["patterns"][0]["stats"]["occurrences"]
            merge_into_rulebook(cand, segments, path)
            second = json.loads(path.read_text())["patterns"][0]["stats"]["occurrences"]
        self.assertEqual(first, 2)
        self.assertEqual(second, 2)


if __name__ == "__main__":
    unittest.main()

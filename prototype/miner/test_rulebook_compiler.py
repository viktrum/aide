import unittest

from rulebook_compiler import (
    compile_class_c_rules,
    compile_escalation,
    compile_rulebook,
    compile_thresholds,
    validate_rule,
    _merge_template_signatures,
)


def sample_aggregates():
    return {
        "template_signatures": [
            {"signature": "CSV ROW # (the event to", "count": 42,
             "example": "CSV ROW 5 (the event to identify)", "sessions": []},
        ],
        "handoff_stats": {
            "handoff_marker_prompt_count": 12,
            "handover_slash_uses": 1,
            "slash_ratio": 0.077,
            "prompts": [],
        },
        "correction_clusters": [
            {"keywords": ["revert", "fork", "spine"], "session_count": 4,
             "prompt_count": 6, "sessions": []},
        ],
    }


class RulebookCompilerTests(unittest.TestCase):
    def test_compile_rulebook_has_v2_schema(self):
        rulebook = compile_rulebook(
            sample_aggregates(),
            {"session_wall_clock_minutes": {"p95": 1800}},
        )

        self.assertEqual(rulebook["version"], 2)
        self.assertIn("generated_at", rulebook)
        self.assertIn("thresholds", rulebook)
        self.assertIn("patterns", rulebook)
        self.assertIn("template_signatures", rulebook)
        self.assertIn("correction_clusters", rulebook)
        self.assertIn("handoff", rulebook)
        self.assertIn("escalation", rulebook)
        self.assertIn("escalation_meta", rulebook)
        self.assertTrue(rulebook["handoff"]["active"])

    def test_compile_escalation_includes_ladders(self):
        esc = compile_escalation({"base_rates": {"webfetch_chain": 0.25}}, existing={})
        self.assertIn("webfetch_chain", esc)
        self.assertIn("ladder", esc["webfetch_chain"])
        self.assertIn("start_rung", esc["webfetch_chain"])

    def test_recompile_migrates_legacy_block_rungs(self):
        existing = {"escalation": {
            "retry_same_prompt": ["block", "block", "stdout"],
        }}
        esc = compile_escalation({}, existing=existing)
        self.assertEqual(esc["retry_same_prompt"]["ladder"],
                         ["transform", "transform", "stdout"])

    def test_escalation_ladders_match_judge(self):
        """Drift test: the judge and the compiler each carry DEFAULT_ESCALATION."""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "judge"))
        import prompt_judge
        import rulebook_compiler
        self.assertEqual(prompt_judge.DEFAULT_ESCALATION,
                         rulebook_compiler.DEFAULT_ESCALATION)

    def test_compiled_rules_validate(self):
        rules = compile_class_c_rules(sample_aggregates())

        self.assertEqual(len(rules), 3)
        for rule in rules:
            self.assertIsNone(validate_rule(rule))
            self.assertEqual(rule["user_status"], "candidate")

    def test_recompile_preserves_confirmed_status(self):
        existing = {
            "patterns": [
                {"id": "c_handoff_ritual", "user_status": "confirmed",
                 "trigger": {"all": []}, "action": {"channel": "stdout",
                 "message": "old message kept for merge test"}},
            ],
        }

        rulebook = compile_rulebook(
            sample_aggregates(), {}, existing_rulebook=existing)

        handoff = next(p for p in rulebook["patterns"]
                       if p["id"] == "c_handoff_ritual")
        self.assertEqual(handoff["user_status"], "confirmed")

    def test_recompile_keeps_label_mined_patterns(self):
        existing = {
            "patterns": [
                {"id": "retry_without_new_information", "user_status": "confirmed",
                 "trigger": {"all": [{"signal": "prompt_similarity_to_failed",
                                      "op": ">=", "value": 0.8}]},
                 "action": {"channel": "inject",
                            "message": "Say what the last attempt got wrong."}},
            ],
        }

        rulebook = compile_rulebook(
            sample_aggregates(), {}, existing_rulebook=existing)

        ids = {p["id"] for p in rulebook["patterns"]}
        self.assertIn("retry_without_new_information", ids)

    def test_thresholds_widen_from_percentiles(self):
        thresholds = compile_thresholds(
            {"session_wall_clock_minutes": {"p95": 2880}})

        self.assertEqual(thresholds["marathon_hours"], 48.0)
        self.assertEqual(thresholds["stale_hours"], 12)

    def test_sparse_aggregates_emit_no_class_c_rules(self):
        sparse = {
            "template_signatures": [],
            "handoff_stats": {"handoff_marker_prompt_count": 2},
            "correction_clusters": [],
        }

        rules = compile_class_c_rules(sparse)

        self.assertEqual(rules, [])

    def test_partial_mine_preserves_prior_template_signature(self):
        existing = {
            "template_signatures": [
                {"signature": "old sig", "count": 99, "example": "x", "sessions": []},
            ],
            "patterns": [],
            "escalation": {},
            "escalation_meta": {},
        }
        partial_agg = {
            "template_signatures": [],
            "handoff_stats": {"handoff_marker_prompt_count": 0},
            "correction_clusters": [],
        }
        rulebook = compile_rulebook(
            partial_agg, {}, existing_rulebook=existing, since_days=7)
        sigs = {t["signature"]: t["count"] for t in rulebook["template_signatures"]}
        self.assertEqual(sigs.get("old sig"), 99)

    def test_partial_mine_skips_base_rate_rung_bump(self):
        full = compile_escalation(
            {"base_rates": {"webfetch_chain": 0.25}}, existing={}, partial=False)
        partial = compile_escalation(
            {"base_rates": {"webfetch_chain": 0.25}}, existing={}, partial=True)
        self.assertEqual(full["webfetch_chain"]["start_rung"], 1)
        self.assertEqual(partial["webfetch_chain"]["start_rung"], 0)

    def test_merge_template_signatures_takes_max_count(self):
        merged = _merge_template_signatures(
            [{"signature": "a", "count": 3}],
            [{"signature": "a", "count": 10}, {"signature": "b", "count": 1}],
        )
        by_sig = {t["signature"]: t["count"] for t in merged}
        self.assertEqual(by_sig["a"], 10)
        self.assertEqual(by_sig["b"], 1)


if __name__ == "__main__":
    unittest.main()

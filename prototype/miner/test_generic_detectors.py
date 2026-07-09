import json
import re
import unittest

from generic_detectors import run_generic_detectors
from session_features import SessionFeatures, UserPromptFeature, normalize_for_detection


REQUIRED_KEYS = {
    "pattern",
    "score",
    "evidence",
    "attribution",
    "confidence",
    "alert_level",
    "suggested_intervention",
    "intervention_class",
    "tier",
}
VALID_ATTRIBUTIONS = {"developer", "agent", "tool_or_platform", "ambiguous"}
VALID_CONFIDENCES = {"low", "medium", "high"}
VALID_ALERT_LEVELS = {"log_only", "passive_insight", "strong_nudge"}
VALID_CLASSES = {"A", "B", "C", "D-report", "D-patch"}
VALID_TIERS = {"T0", "T1"}


def prompt(
    text,
    *,
    entry_index=0,
    imperative_count=1,
    has_file_ref=False,
    is_vague=False,
    has_plan_marker=False,
    has_verify_marker=False,
    has_review_marker=False,
    has_clear_marker=False,
    has_typo_marker=False,
    is_status_ack=False,
    question_mark_count=0,
    has_handoff_marker=False,
):
    text_norm = normalize_for_detection(text)
    return UserPromptFeature(
        entry_index=entry_index,
        turn_index=0,
        text=text,
        text_norm=text_norm,
        word_count=len(re.findall(r"[a-z0-9']+", text_norm)),
        imperative_count=0 if is_status_ack else imperative_count,
        has_file_ref=has_file_ref,
        is_vague=is_vague,
        has_plan_marker=has_plan_marker,
        has_verify_marker=has_verify_marker,
        has_review_marker=has_review_marker,
        has_clear_marker=has_clear_marker,
        has_typo_marker=has_typo_marker,
        is_status_ack=is_status_ack,
        question_mark_count=question_mark_count,
        has_handoff_marker=has_handoff_marker,
    )


def features_with(prompts=None, **overrides):
    features = SessionFeatures(
        session_id="test-session",
        path="test-session.jsonl",
        user_prompts=prompts or [],
    )
    for key, value in overrides.items():
        setattr(features, key, value)
    return features


def patterns(findings):
    return {finding["pattern"] for finding in findings}


def finding_by_pattern(findings, pattern):
    for finding in findings:
        if finding["pattern"] == pattern:
            return finding
    return None


class GenericDetectorTests(unittest.TestCase):
    def test_findings_have_required_shape_and_valid_enum_values(self):
        features = features_with(
            [prompt("do it", is_vague=True)],
            file_edit_count=1,
            verification_command_count=0,
            review_marker_count=0,
        )

        findings = run_generic_detectors(features)

        self.assertTrue(findings)
        for finding in findings:
            self.assertEqual(set(finding), REQUIRED_KEYS)
            self.assertIsInstance(finding["pattern"], str)
            self.assertIsInstance(finding["score"], (int, float))
            self.assertIsInstance(finding["evidence"], list)
            self.assertTrue(all(isinstance(item, str) for item in finding["evidence"]))
            self.assertIn(finding["attribution"], VALID_ATTRIBUTIONS)
            self.assertIn(finding["confidence"], VALID_CONFIDENCES)
            self.assertIn(finding["alert_level"], VALID_ALERT_LEVELS)
            self.assertIn(finding["intervention_class"], VALID_CLASSES)
            self.assertIn(finding["tier"], VALID_TIERS)
            self.assertIsInstance(finding["suggested_intervention"], str)
            json.dumps(finding)

    def test_missing_verification_criteria_requires_edits(self):
        no_edits = run_generic_detectors(features_with(file_edit_count=0))
        with_edits = run_generic_detectors(
            features_with(
                file_edit_count=1,
                verification_command_count=0,
                review_marker_count=0,
            )
        )

        self.assertNotIn("missing_verification_criteria", patterns(no_edits))
        self.assertIn("missing_verification_criteria", patterns(with_edits))

    def test_repo_context_dumping_uses_structural_signal(self):
        prompt_only = run_generic_detectors(
            features_with([prompt("dump the whole repo context before editing")])
        )
        structural = run_generic_detectors(
            features_with(file_edit_count=1, files_read_before_first_edit=26)
        )

        self.assertNotIn("repo_context_dumping", patterns(prompt_only))
        self.assertIn("repo_context_dumping", patterns(structural))

    def test_repo_context_dumping_ignores_read_only_sessions(self):
        findings = run_generic_detectors(
            features_with(
                file_edit_count=0,
                files_read_before_first_edit=60,
                tool_output_chars_before_first_edit=120_000,
            )
        )

        self.assertNotIn("repo_context_dumping", patterns(findings))

    def test_status_acks_do_not_trigger_vague_underprompting(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt("go ahead", is_status_ack=True),
                    prompt("continue", is_status_ack=True),
                    prompt("status?", is_status_ack=True),
                ]
            )
        )

        self.assertNotIn("vague_underprompting", patterns(findings))

    def test_do_it_triggers_vague_underprompting(self):
        findings = run_generic_detectors(
            features_with([prompt("do it", is_vague=True)])
        )

        self.assertIn("vague_underprompting", patterns(findings))

    def test_edit_before_plan_triggers_for_complex_prompt_edits_and_no_plan(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt(
                        "Implement auth.py changes, update the login component, add tests, and then fix routing",
                        imperative_count=4,
                        has_file_ref=True,
                    )
                ],
                file_edit_count=1,
                planning_marker_count=0,
            )
        )

        finding = finding_by_pattern(findings, "edit_before_plan")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["attribution"], "ambiguous")
        self.assertIn(
            "no user planning marker observed before edits",
            finding["evidence"],
        )
        self.assertIn("before edits begin", finding["suggested_intervention"])
        self.assertNotIn("assistant", finding["suggested_intervention"].lower())

    def test_prompt_quality_findings_in_subagent_sessions_are_agent_attributed(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt(
                        "Review auth.py and also check billing.py after that",
                        imperative_count=3,
                        has_file_ref=True,
                    )
                ],
                session_id="agent-test",
                context_window=200_000,
                max_context_tokens=90_000,
            )
        )

        finding = finding_by_pattern(findings, "kitchen_sink_session")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["attribution"], "agent")

    def test_correction_accumulation_requires_specific_correction_prompts(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt("that's wrong, use the auth flow"),
                    prompt("you missed the token refresh case"),
                ]
            )
        )

        self.assertIn("correction_accumulation", patterns(findings))

    def test_correction_accumulation_ignores_benign_again_and_no_prompts(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt("run tests again"),
                    prompt("no changes needed"),
                ]
            )
        )

        self.assertNotIn("correction_accumulation", patterns(findings))

    def test_correction_accumulation_ignores_sessions_with_clear_marker(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt("that's wrong, use the auth flow"),
                    prompt("you missed the token refresh case"),
                ],
                clear_or_compact_marker_count=1,
            )
        )

        self.assertNotIn("correction_accumulation", patterns(findings))

    def test_typo_induced_ambiguity_triggers_for_explicit_typo_marker(self):
        findings = run_generic_detectors(
            features_with(
                [prompt("typo, I meant payment.py", has_typo_marker=True)]
            )
        )

        self.assertIn("typo_induced_ambiguity", patterns(findings))

    def test_typo_induced_ambiguity_ignores_unpaired_typo_marker(self):
        findings = run_generic_detectors(
            features_with(
                [prompt("there is a typo in the docs", has_typo_marker=True)]
            )
        )

        self.assertNotIn("typo_induced_ambiguity", patterns(findings))

    def test_error_dump_without_repro_or_runtime_context_triggers_on_raw_prompt(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt(
                        "my react app crashes when I click the submit button, here's the error: "
                        "TypeError: Cannot read properties of undefined (reading 'map')"
                    )
                ]
            )
        )

        self.assertIn(
            "error_dump_without_repro_or_runtime_context",
            patterns(findings),
        )

    def test_error_dump_without_repro_or_runtime_context_ignores_plain_prose_failing(self):
        findings = run_generic_detectors(
            features_with(
                [
                    prompt(
                        "What does this mean? No farmer is auto-labelled failing. "
                        "Judgment stays human."
                    )
                ]
            )
        )

        self.assertNotIn(
            "error_dump_without_repro_or_runtime_context",
            patterns(findings),
        )

    def test_error_dump_without_repro_or_runtime_context_ignores_runtime_context(self):
        contextual_prompts = [
            "TypeError: Cannot read properties of undefined. Initial state is items=[] before submit.",
            "TypeError: Cannot read properties of undefined. API response data shape is {items: []}.",
            "TypeError on submit. Expected the list to render; actual behavior is a crash.",
            "TypeError: Cannot read properties of undefined. Steps to reproduce: open form, submit empty values.",
        ]

        for text in contextual_prompts:
            with self.subTest(text=text):
                findings = run_generic_detectors(features_with([prompt(text)]))
                self.assertNotIn(
                    "error_dump_without_repro_or_runtime_context",
                    patterns(findings),
                )


def bash(norm, after_error=False):
    return {"entry_index": 0, "norm": norm, "after_error": after_error}


class NewDetectorTests(unittest.TestCase):
    def test_command_retry_spiral_fires_on_three_identical_commands(self):
        findings = run_generic_detectors(features_with(
            bash_commands=[
                bash("bun run typecheck"),
                bash("bun run typecheck", after_error=True),
                bash("bun run typecheck", after_error=True),
            ],
        ))

        finding = finding_by_pattern(findings, "command_retry_spiral")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["confidence"], "high")
        self.assertEqual(finding["intervention_class"], "B")

    def test_command_retry_spiral_ignores_run_of_two(self):
        findings = run_generic_detectors(features_with(
            bash_commands=[
                bash("bun run typecheck"),
                bash("bun run typecheck"),
                bash("git status"),
            ],
        ))

        self.assertNotIn("command_retry_spiral", patterns(findings))

    def test_command_retry_spiral_ignores_polling_commands(self):
        findings = run_generic_detectors(features_with(
            bash_commands=[
                bash("sleep 30 && check"),
                bash("sleep 30 && check"),
                bash("sleep 30 && check"),
            ],
        ))

        self.assertNotIn("command_retry_spiral", patterns(findings))

    def test_marathon_fires_on_prompt_count_without_clear(self):
        findings = run_generic_detectors(features_with(
            [prompt(f"task {i}") for i in range(61)],
            slash_commands=[],
        ))

        self.assertIn("marathon_session_sprawl", patterns(findings))

    def test_marathon_ignores_subagent_sessions(self):
        findings = run_generic_detectors(features_with(
            [prompt(f"task {i}") for i in range(61)],
            is_subagent=True,
        ))

        self.assertNotIn("marathon_session_sprawl", patterns(findings))

    def test_marathon_ignores_sessions_with_clear(self):
        findings = run_generic_detectors(features_with(
            [prompt(f"task {i}") for i in range(61)],
            slash_commands=["/clear"],
        ))

        self.assertNotIn("marathon_session_sprawl", patterns(findings))

    def test_question_stacking_fires_on_three_questions(self):
        findings = run_generic_detectors(features_with(
            [prompt("what is x? why y? how z?", question_mark_count=3)],
        ))

        finding = finding_by_pattern(findings, "question_stacking")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["intervention_class"], "A")

    def test_question_stacking_ignores_two_questions(self):
        findings = run_generic_detectors(features_with(
            [prompt("what is x? why y?", question_mark_count=2)],
        ))

        self.assertNotIn("question_stacking", patterns(findings))

    def test_interrupt_churn_fires_on_three_non_corrective_interrupts(self):
        findings = run_generic_detectors(features_with(
            [prompt("try a different angle", entry_index=10)],
            interrupt_entry_indexes=[2, 5, 8],
        ))

        self.assertIn("interrupt_churn", patterns(findings))

    def test_interrupt_churn_ignores_corrective_interrupts(self):
        findings = run_generic_detectors(features_with(
            [
                prompt("that's wrong, use the auth flow", entry_index=3),
                prompt("wrong file, edit billing.py", entry_index=6),
                prompt("you missed the token refresh", entry_index=9),
            ],
            interrupt_entry_indexes=[2, 5, 8],
        ))

        self.assertNotIn("interrupt_churn", patterns(findings))

    def test_unattended_long_run_escalates_with_auto_mode_and_risky_command(self):
        low = run_generic_detectors(features_with(away_summary_count=1))
        high = run_generic_detectors(features_with(
            away_summary_count=1,
            permission_modes=["auto"],
            bash_commands=[bash("rm -rf build && make")],
        ))

        low_finding = finding_by_pattern(low, "unattended_long_run")
        high_finding = finding_by_pattern(high, "unattended_long_run")
        self.assertEqual(low_finding["confidence"], "low")
        self.assertEqual(high_finding["confidence"], "high")

    def test_context_stuffing_fires_on_jump_and_on_peak(self):
        jump = run_generic_detectors(features_with(
            cache_read_series=[30_000, 70_000]))
        peak = run_generic_detectors(features_with(
            cache_read_series=[151_000]))
        flat = run_generic_detectors(features_with(
            cache_read_series=[79_000, 79_500]))

        self.assertIn("context_stuffing_single_turn", patterns(jump))
        self.assertIn("context_stuffing_single_turn", patterns(peak))
        self.assertNotIn("context_stuffing_single_turn", patterns(flat))

    def test_webfetch_chain_fires_on_run_of_twelve(self):
        fires = run_generic_detectors(features_with(web_tool_runs=[1, 12]))
        quiet = run_generic_detectors(features_with(web_tool_runs=[2, 10]))

        self.assertIn("webfetch_chain", patterns(fires))
        self.assertNotIn("webfetch_chain", patterns(quiet))

    def test_dangerous_auto_accept_needs_auto_mode_and_risky_command(self):
        fires = run_generic_detectors(features_with(
            permission_modes=["auto"],
            bash_commands=[bash("git reset --hard HEAD~3")],
        ))
        no_auto = run_generic_detectors(features_with(
            permission_modes=["plan"],
            bash_commands=[bash("git reset --hard HEAD~3")],
        ))
        no_risky = run_generic_detectors(features_with(
            permission_modes=["auto"],
            bash_commands=[bash("git status")],
        ))

        self.assertIn("dangerous_auto_accept", patterns(fires))
        self.assertNotIn("dangerous_auto_accept", patterns(no_auto))
        self.assertNotIn("dangerous_auto_accept", patterns(no_risky))

    def test_manual_handoff_marker_skips_slash_command_users(self):
        fires = run_generic_detectors(features_with(
            [prompt("lets prepare for next session handoff", has_handoff_marker=True)],
        ))
        quiet = run_generic_detectors(features_with(
            [prompt("lets prepare for next session handoff", has_handoff_marker=True)],
            slash_commands=["/handover"],
        ))

        fired = finding_by_pattern(fires, "manual_handoff_marker")
        self.assertIsNotNone(fired)
        self.assertEqual(fired["intervention_class"], "C")
        self.assertNotIn("manual_handoff_marker", patterns(quiet))


if __name__ == "__main__":
    unittest.main()

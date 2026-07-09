import os
import unittest
import unittest.mock

import optimizer


class BuilderTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.dict(os.environ, {"AIDE_OPTIMIZER_LLM": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_r2_retry_packet_carries_error_and_original(self):
        sig = {"last_run_error_snippet": "AssertionError: expected 200 got 500"}
        result = optimizer.optimize("R2", "fix the webhook in stripe.py", sig, {})
        self.assertIsNotNone(result)
        self.assertIn("AssertionError: expected 200 got 500", result["text"])
        self.assertIn("fix the webhook in stripe.py", result["text"])
        self.assertIn("previous_attempt_summary", result["text"])
        self.assertEqual(result["method"], "deterministic")

    def test_r21_wraps_trace_in_error_report(self):
        trace = "TypeError: undefined is not a function\n  at foo (bar.js:12)"
        result = optimizer.optimize("R21", trace, {}, {})
        self.assertIn("<error_report>", result["text"])
        self.assertIn(trace, result["text"])
        self.assertIn("root cause", result["text"])

    def test_r6_splits_bundled_asks_into_numbered_tasks(self):
        prompt = ("add input validation to the signup form; also write unit tests "
                  "for the validator and then update the API docs with the new fields")
        result = optimizer.optimize("R6", prompt, {}, {})
        self.assertIsNotNone(result)
        self.assertIn("1. ", result["text"])
        self.assertIn("2. ", result["text"])
        self.assertIn("one at a time", result["text"])

    def test_r6_returns_none_when_nothing_to_split(self):
        self.assertIsNone(optimizer.optimize("R6", "short ask", {}, {}))

    def test_r13_numbers_every_question(self):
        prompt = "what does the miner do? why is labelling slow? how do I rerun it?"
        result = optimizer.optimize("R13", prompt, {}, {})
        self.assertIn("1. ", result["text"])
        self.assertIn("3. ", result["text"])
        self.assertIn("numbered checklist", result["text"])

    def test_r5_wraps_vague_opener(self):
        result = optimizer.optimize("R5", "fix the login thing", {}, {})
        self.assertIn("<request>", result["text"])
        self.assertIn("fix the login thing", result["text"])
        self.assertIn("ONE targeted question", result["text"])

    def test_unknown_rule_returns_none(self):
        self.assertIsNone(optimizer.optimize("R99", "anything at all", {}, {}))


class PackagingTests(unittest.TestCase):
    def _transform(self):
        return {"rule": "R21", "label": "raw error dump structured into a bug report",
                "text": "<error_report>...</error_report>", "method": "deterministic",
                "latency_ms": 1}

    def test_context_packet_is_authoritative_and_tagged(self):
        ctx = optimizer.transform_context(self._transform())
        self.assertIn("PROMPT OPTIMISED", ctx)
        self.assertIn("canonical version", ctx)
        self.assertIn('<optimized_prompt rule="R21">', ctx)
        self.assertIn("Do not mention", ctx)

    def test_notice_is_one_line_with_bypass_hint(self):
        line = optimizer.transform_notice(self._transform())
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("prompt optimised", line)
        self.assertIn("*", line)


class RewriteValidationTests(unittest.TestCase):
    def test_rejects_empty_and_fenced_output(self):
        self.assertFalse(optimizer._valid_rewrite("p", "skeleton", ""))
        self.assertFalse(optimizer._valid_rewrite("p", "skeleton", "```\ncode\n```"))

    def test_rejects_rewrite_that_drops_file_refs(self):
        prompt = "fix the handler in src/webhooks/stripe.py"
        self.assertFalse(optimizer._valid_rewrite(prompt, "skeleton", "fix the handler"))
        self.assertTrue(optimizer._valid_rewrite(
            prompt, "skeleton", "fix the handler in src/webhooks/stripe.py properly"))

    def test_rejects_runaway_length(self):
        self.assertFalse(optimizer._valid_rewrite("p", "tiny", "x" * 5000))

    def test_llm_off_never_polishes(self):
        with unittest.mock.patch.dict(os.environ, {"AIDE_OPTIMIZER_LLM": "off"}):
            with unittest.mock.patch.object(optimizer, "_call_api") as api, \
                    unittest.mock.patch.object(optimizer, "_call_cli") as cli:
                result = optimizer.optimize(
                    "R5", "improve the dashboard", {}, {})
        api.assert_not_called()
        cli.assert_not_called()
        self.assertEqual(result["method"], "deterministic")

    def test_lexical_rule_uses_polish_when_valid(self):
        with unittest.mock.patch.dict(os.environ, {"AIDE_OPTIMIZER_LLM": "cli"}):
            with unittest.mock.patch.object(
                    optimizer, "_call_cli",
                    return_value="Clarify and fix the dashboard rendering issue."):
                result = optimizer.optimize("R5", "improve the dashboard", {}, {})
        self.assertEqual(result["method"], "haiku")
        self.assertIn("dashboard", result["text"])

    def test_structural_rule_never_calls_llm(self):
        with unittest.mock.patch.dict(os.environ, {"AIDE_OPTIMIZER_LLM": "cli"}):
            with unittest.mock.patch.object(optimizer, "_call_cli") as cli:
                optimizer.optimize("R21", "TypeError: boom\n  at f (a.js:1)", {}, {})
        cli.assert_not_called()


if __name__ == "__main__":
    unittest.main()

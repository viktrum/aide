import tempfile
import unittest
from pathlib import Path

from config_lint import render_config_report, run_config_lints
from session_features import SessionFeatures, ToolEventFeature


def tool_event(name):
    return ToolEventFeature(
        entry_index=0, name=name, input_text="", output_text="",
        is_error=False, is_read=False, is_edit=False, is_verification=False,
        output_chars=0)


def session_pair(session_id, project, *, tool_names=(), hook_errors=()):
    features = SessionFeatures(session_id=session_id, path=f"{session_id}.jsonl")
    features.tool_events = [tool_event(name) for name in tool_names]
    features.hook_error_samples = list(hook_errors)
    return features, {"project": project}


def patterns(findings):
    return {finding["pattern"] for finding in findings}


class ConfigLintTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def run_lints(self, sessions):
        return run_config_lints([self.repo], sessions, home=self.home)

    def test_bloated_claude_md_with_embedded_scan_fires_both_lints(self):
        body = "\n".join(
            ["# CLAUDE.md", "Run `grep -r TODO .` before every task."]
            + [f"IMPORTANT: rule number {i}, NEVER skip it." for i in range(30)]
            + ["filler line"] * 250
        )
        (self.repo / "CLAUDE.md").write_text(body)

        findings = self.run_lints([])

        found = patterns(findings)
        self.assertIn("instruction_file_embedded_scans", found)
        self.assertIn("bloated_instruction_files", found)

    def test_short_clean_claude_md_is_quiet(self):
        (self.repo / "CLAUDE.md").write_text("# CLAUDE.md\nBe concise.\n")

        findings = self.run_lints([])

        self.assertEqual(findings, [])

    def test_verbose_checklist_in_list_items_fires(self):
        body = "\n".join(
            ["# CLAUDE.md"]
            + [f"- MUST check item {i} and NEVER skip it" for i in range(15)]
        )
        (self.repo / "CLAUDE.md").write_text(body)

        findings = self.run_lints([])

        self.assertIn("verbose_verification_checklists", patterns(findings))

    def test_missing_claudeignore_with_node_modules_fires(self):
        (self.repo / "node_modules").mkdir()
        (self.repo / "dist").mkdir()

        findings = self.run_lints([])

        finding = next(f for f in findings
                       if f["pattern"] == "incomplete_ignore_configuration")
        self.assertIn("heavy_dirs=node_modules,dist", finding["evidence"])

    def test_claudeignore_present_is_quiet(self):
        (self.repo / "node_modules").mkdir()
        (self.repo / ".claudeignore").write_text("node_modules/\n")

        findings = self.run_lints([])

        self.assertNotIn("incomplete_ignore_configuration", patterns(findings))

    def test_unused_mcp_server_fires_and_used_one_does_not(self):
        claude_dir = self.repo / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            '{"mcpServers": {"github": {}, "playwright": {}}}')
        sessions = [session_pair(
            "s0", str(self.repo),
            tool_names=["mcp__github__create_issue", "Bash"])]

        findings = self.run_lints(sessions)

        finding = next(f for f in findings
                       if f["pattern"] == "always_on_mcp_servers")
        self.assertIn("never_invoked=playwright", finding["evidence"])

    def test_hook_errors_scoped_to_repo_fire(self):
        sessions = [
            session_pair("s0", str(self.repo),
                         hook_errors=["PreToolUse:Write hook returned invalid JSON"]),
            session_pair("s1", "/some/other/repo",
                         hook_errors=["hook failed elsewhere"]),
        ]

        findings = self.run_lints(sessions)

        finding = next(f for f in findings
                       if f["pattern"] == "broken_hook_configuration")
        self.assertIn("sessions_with_hook_errors=1", finding["evidence"])

    def test_all_findings_are_d_patch_t0(self):
        (self.repo / "node_modules").mkdir()

        findings = self.run_lints([])

        self.assertTrue(findings)
        for finding in findings:
            self.assertEqual(finding["intervention_class"], "D-patch")
            self.assertEqual(finding["tier"], "T0")

    def test_report_renders_per_repo_sections(self):
        (self.repo / "node_modules").mkdir()
        findings = self.run_lints([])

        report = render_config_report(findings, "2026-07-08")

        self.assertIn(f"## {self.repo}", report)
        self.assertIn("incomplete_ignore_configuration", report)
        self.assertIn("Suggested patch:", report)


if __name__ == "__main__":
    unittest.main()

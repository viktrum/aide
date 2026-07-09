import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

JUDGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(JUDGE_DIR))
JUDGE = JUDGE_DIR / "prompt_judge.py"
POSTTOOL = JUDGE_DIR / "posttool_record.py"
PRETOOL = JUDGE_DIR / "pretool_gate.py"
SESSION_START = JUDGE_DIR / "session_start.py"
PRECOMPACT = JUDGE_DIR / "precompact.py"
STOP_VERIFY = JUDGE_DIR / "stop_verify.py"
SCHEDULED_MINE = JUDGE_DIR / "scheduled_mine.py"


def run_hook(script, payload, env=None, data_dir=None):
    run_env = dict(env or {})
    run_env.setdefault("AIDE_OPTIMIZER_LLM", "off")  # no LLM calls in tests
    if data_dir:
        run_env["CLAUDE_JUDGE_HOME"] = str(data_dir)
    proc = subprocess.run(
        ["python3", str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, **run_env},
        timeout=5,
    )
    stdout = proc.stdout.strip()
    return proc.returncode, json.loads(stdout) if stdout else None


def run_judge(payload, env=None, data_dir=None):
    return run_hook(JUDGE, payload, env=env, data_dir=data_dir)


def write_transcript(path, entries):
    with open(path, "w") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def web_chain_entries(n=12):
    entries = [{"type": "user", "message": {"content": "research the API docs"}}]
    content = []
    for i in range(n):
        content.append({"type": "tool_use", "name": "WebFetch",
                        "input": {"url": f"https://example.com/{i}"}})
    entries.append({"type": "assistant", "message": {"content": content}})
    entries.append({"type": "user", "message": {"content": "keep going"}})
    return entries


class PromptJudgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "judge-data"
        self.data_dir.mkdir()
        self.transcript = self.root / "session.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def payload(self, prompt, entries=None, session_id="test-session"):
        if entries is not None:
            write_transcript(self.transcript, entries)
        return {
            "prompt": prompt,
            "session_id": session_id,
            "cwd": str(self.root),
            "transcript_path": str(self.transcript),
        }

    def test_stale_resumption_fires_r11(self):
        entries = [
            {"type": "user", "timestamp": "2026-07-01T08:00:00Z",
             "message": {"content": "start the billing migration"}},
            {"type": "assistant", "timestamp": "2026-07-01T08:01:00Z",
             "message": {"model": "claude-sonnet", "usage": {
                 "input_tokens": 5000, "cache_read_input_tokens": 25000}}},
            {"type": "user", "timestamp": "2026-07-02T10:00:00Z",
             "message": {"content": "continue"}},
        ]
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {"stale_hours": 12, "stale_carried_tokens": 20000},
        }))
        code, out = run_judge(
            self.payload("continue where we left off", entries),
            data_dir=self.data_dir,
        )
        self.assertEqual(code, 0)
        self.assertIn("systemMessage", out or {})
        self.assertIn("carried tokens", out["systemMessage"])

    def test_compaction_dedup_suppresses_r11(self):
        entries = [
            {"type": "user", "timestamp": "2026-07-01T08:00:00Z",
             "message": {"content": "start work"}},
            {"type": "assistant", "timestamp": "2026-07-01T08:01:00Z",
             "message": {"model": "claude-sonnet", "usage": {
                 "input_tokens": 5000, "cache_read_input_tokens": 25000}}},
            {"type": "user", "timestamp": "2026-07-02T10:00:00Z",
             "message": {"content": (
                 "This session is being continued from a previous conversation")}},
            {"type": "user", "timestamp": "2026-07-02T10:01:00Z",
             "message": {"content": "ok go"}},
        ]
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {"stale_hours": 1, "stale_carried_tokens": 10000},
        }))
        code, out = run_judge(self.payload("ok go", entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        msg = (out or {}).get("systemMessage", "")
        self.assertNotIn("Resuming after", msg)

    def test_fatigue_budget_keeps_one_notice(self):
        entries = [
            {"type": "user", "message": {"content": "fix the auth bug in login.py"}},
            {"type": "assistant", "message": {"model": "claude-sonnet", "usage": {
                "input_tokens": 90000, "cache_read_input_tokens": 0}}},
            {"type": "user", "message": {"content": "new task: redesign the dashboard UI completely"}},
        ]
        code, out = run_judge(
            self.payload("new task: redesign the dashboard UI completely", entries),
            data_dir=self.data_dir,
        )
        self.assertEqual(code, 0)
        if out and "systemMessage" in out:
            notice_count = out["systemMessage"].count("[judge]")
            self.assertLessEqual(notice_count, 1)

    def test_question_stacking_injects(self):
        entries = [{"type": "user", "message": {"content": "earlier work"}}]
        code, out = run_judge(
            self.payload("what is x? why y? how z?", entries),
            data_dir=self.data_dir,
        )
        self.assertEqual(code, 0)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn("numbered", ctx)

    def test_trivial_prompt_exits_silently(self):
        code, out = run_judge(self.payload("ok"), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_bypass_prefix_exits_silently(self):
        entries = [{"type": "user", "message": {"content": "earlier"}}]
        code, out = run_judge(self.payload("* implement the whole feature now", entries),
                              data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_webfetch_chain_injects_r18(self):
        entries = web_chain_entries(12)
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {"web_chain_min": 12},
        }))
        code, out = run_judge(self.payload("continue with the integration", entries),
                              data_dir=self.data_dir)
        self.assertEqual(code, 0)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn("source_policy", ctx)

    def test_error_without_repro_transforms_r21(self):
        entries = [{"type": "user", "message": {"content": "earlier"}}]
        prompt = "TypeError: undefined is not a function\n  at foo (bar.js:12)"
        code, out = run_judge(self.payload(prompt, entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn("error_report", ctx)
        self.assertIn("<optimized_prompt", ctx)
        self.assertIn("prompt optimised", (out or {}).get("systemMessage", ""))

    def test_transform_keeps_single_visible_line(self):
        entries = [{"type": "user", "message": {"content": "earlier"}}]
        prompt = "TypeError: undefined is not a function\n  at foo (bar.js:12)"
        code, out = run_judge(self.payload(prompt, entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        msg = (out or {}).get("systemMessage", "")
        self.assertEqual(len(msg.splitlines()), 1)

    def test_bypass_env_guard_exits_silently(self):
        entries = [{"type": "user", "message": {"content": "earlier"}}]
        code, out = run_judge(
            self.payload("what is x? why y? how z?", entries),
            env={"AIDE_JUDGE_BYPASS": "1"}, data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_r2_transforms_repeat_failed_prompt(self):
        failed_prompt = "fix the payment webhook handler in stripe.py"
        entries = [
            {"type": "user", "message": {"content": failed_prompt}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
                {"type": "tool_result", "is_error": True, "content": "FAIL"},
            ]}},
            {"type": "user", "message": {"content": failed_prompt}},
        ]
        code, out = run_judge(self.payload(failed_prompt, entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        # 10-star UX: never blocks — the prompt passes through with a rewrite.
        self.assertIsNone((out or {}).get("decision"))
        self.assertIn("prompt optimised", (out or {}).get("systemMessage", ""))
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn("<optimized_prompt", ctx)
        self.assertIn("previous_attempt_summary", ctx)
        self.assertIn(failed_prompt, ctx)
        self.assertTrue((self.data_dir / "pending_transform.md").exists())

    def test_r2_demotes_after_two_overrides(self):
        failed_prompt = "fix the payment webhook handler in stripe.py"
        entries = [
            {"type": "user", "message": {"content": failed_prompt}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
                {"type": "tool_result", "is_error": True, "content": "FAIL"},
            ]}},
        ]
        marks = {
            "test-session": {
                "R2": {"override_count": 2},
            }
        }
        (self.data_dir / "session_marks.json").write_text(json.dumps(marks))
        code, out = run_judge(self.payload(failed_prompt, entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertNotEqual((out or {}).get("decision"), "block")
        self.assertIn("systemMessage", out or {})

    def test_inject_merge_carry_forward(self):
        reads = []
        for i in range(6):
            reads.append({"type": "tool_use", "name": "Read",
                          "input": {"file_path": f"/src/f{i}.py"}})
        web = [{"type": "tool_use", "name": "WebFetch",
                "input": {"url": f"https://example.com/{i}"}} for i in range(12)]
        entries = [
            {"type": "user", "message": {"content": "start integration work"}},
            {"type": "assistant", "message": {
                "content": reads + web + [
                    {"type": "tool_use", "name": "Edit",
                     "input": {"file_path": "/src/f0.py"}}],
                "usage": {"cache_read_input_tokens": 200000}}},
            {"type": "user", "message": {"content": "continue"}},
        ]
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {
                "web_chain_min": 12,
                "reads_before_edit_min": 5,
                "stuffing_peak_cache": 150000,
            },
        }))
        code, out = run_judge(self.payload("keep going please", entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn("carry_forward", ctx)

    def test_verification_packet_r7(self):
        entries = [
            {"type": "user", "message": {"content": "start"}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 1000}}},
            {"type": "user", "message": {"content": "more"}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 1000}}},
            {"type": "user", "message": {"content": "again"}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 1000}}},
        ]
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "verify_commands": ["npm test", "npm run lint"],
        }))
        code, out = run_judge(self.payload(
            "implement the auth middleware with unit tests and error handling",
            entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        self.assertIn("verification", ctx)
        self.assertIn("npm test", ctx)

    def test_r3_r11_notice_alignment(self):
        entries = [
            {"type": "user", "timestamp": "2026-07-01T08:00:00Z",
             "message": {"content": "start the billing migration"}},
            {"type": "assistant", "timestamp": "2026-07-01T08:01:00Z",
             "message": {"model": "claude-sonnet", "usage": {
                 "input_tokens": 5000, "cache_read_input_tokens": 25000}}},
            {"type": "user", "timestamp": "2026-07-02T10:00:00Z",
             "message": {"content": "no that is wrong try again still broken"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": "FAIL"},
            ]}},
            {"type": "user", "timestamp": "2026-07-02T10:05:00Z",
             "message": {"content": "continue"}},
        ]
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {"stale_hours": 1, "stale_carried_tokens": 10000},
        }))
        code, out = run_judge(self.payload("continue where we left off", entries), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        msg = (out or {}).get("systemMessage", "")
        self.assertIn("Resuming after", msg)
        self.assertNotIn("consecutive failures", msg)

    def test_bypass_resets_web_chain(self):
        entries = [{"type": "user", "message": {"content": "research docs"}}]
        write_transcript(self.transcript, entries)
        state_dir = self.data_dir / "session_state"
        state_dir.mkdir(parents=True)
        (state_dir / "bypass-session.json").write_text(json.dumps({
            "web_chain": 12, "research_intent": False, "turn": 1,
        }))
        payload = {
            "prompt": "* continue fetching more pages please",
            "session_id": "bypass-session",
            "cwd": str(self.root),
            "transcript_path": str(self.transcript),
        }
        run_judge(payload, data_dir=self.data_dir)
        state = json.loads((state_dir / "bypass-session.json").read_text())
        self.assertEqual(state.get("web_chain"), 0)
        code, out = run_hook(PRETOOL, {
            "session_id": "bypass-session",
            "cwd": str(self.root),
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.com/x"},
        }, data_dir=self.data_dir)
        self.assertEqual(code, 0)
        decision = ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")
        self.assertNotEqual(decision, "deny")

    def test_internal_tool_signal(self):
        import prompt_judge as pj
        sig = pj.compute_signals(
            "Use mcp__browser__goto on each URL and return text",
            [], str(self.root), {})
        self.assertEqual(sig["prompt_mentions_internal_tool_names"], 1)

    def test_performance_under_150ms(self):
        entries = []
        for i in range(250):
            entries.append({"type": "user", "timestamp": f"2026-07-01T10:{i%60:02d}:00Z",
                            "message": {"content": f"task {i} with some context"}})
            entries.append({"type": "assistant", "timestamp": f"2026-07-01T10:{i%60:02d}:01Z",
                            "message": {"model": "claude-sonnet", "usage": {
                                "input_tokens": 1000 + i,
                                "cache_read_input_tokens": 5000 + i * 10}}})
        write_transcript(self.transcript, entries)
        start = time.perf_counter()
        proc = subprocess.run(
            ["python3", str(JUDGE)],
            input=json.dumps(self.payload("implement the next feature step")),
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "CLAUDE_JUDGE_HOME": str(self.data_dir)},
            timeout=5,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertEqual(proc.returncode, 0)
        self.assertLess(elapsed_ms, 150)


class HooksV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "judge-data"
        self.data_dir.mkdir()
        # Most V2 tests exercise the full scope; default-scope tests remove this.
        (self.data_dir / "config.json").write_text('{"scope": "full"}')
        self.transcript = self.root / "session.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def payload(self, **kwargs):
        base = {
            "session_id": "v2-session",
            "cwd": str(self.root),
            "transcript_path": str(self.transcript),
        }
        base.update(kwargs)
        return base

    def _gate(self, command, mode="acceptEdits"):
        code, out = run_hook(PRETOOL, self.payload(
            tool_name="Bash", tool_input={"command": command},
            permission_mode=mode), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")

    def test_default_scope_is_prompt_only(self):
        (self.data_dir / "config.json").unlink()
        # Even the catastrophic case passes through: tool gates are opt-in.
        self.assertIsNone(self._gate("rm -rf /"))

    def test_prompt_scope_disables_stop_verify(self):
        (self.data_dir / "config.json").write_text('{"scope": "prompt"}')
        code, out = run_hook(STOP_VERIFY, self.payload(), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_pretool_asks_on_hard_reset_under_auto_accept(self):
        self.assertEqual(self._gate("git reset --hard origin/main"), "ask")

    def test_pretool_denies_rm_rf_on_root_or_home(self):
        self.assertEqual(self._gate("rm -rf /"), "deny")
        self.assertEqual(self._gate("sudo rm -rf $HOME"), "deny")

    def test_pretool_asks_on_rm_rf_outside_workspace(self):
        self.assertEqual(self._gate("rm -rf /Users/someone/project"), "ask")
        self.assertEqual(self._gate('rm -rf "$SCRATCH/clone"'), "ask")
        self.assertEqual(self._gate("rm -rf ../sibling"), "ask")

    def test_pretool_allows_routine_rm_rf(self):
        self.assertIsNone(self._gate("rm -rf node_modules dist"))
        self.assertIsNone(self._gate("rm -rf /tmp/scratch-clone"))

    def test_pretool_allows_destructive_text_as_data(self):
        heredoc = ("cat > notes.md <<'EOF'\n"
                   "To clean up run: rm -rf ./build\n"
                   "EOF")
        self.assertIsNone(self._gate(heredoc))
        self.assertIsNone(self._gate('git commit -m "docs: remove rm -rf example"'))
        self.assertIsNone(self._gate('grep -rn "git reset --hard" docs/'))

    def test_pretool_still_denies_heredoc_fed_to_shell(self):
        cmd = ("bash <<'EOF'\n"
               "rm -rf /\n"
               "EOF")
        self.assertEqual(self._gate(cmd), "deny")

    def test_all_hooks_exit_silently_under_bypass(self):
        """Optimizer CLI child sessions (AIDE_JUDGE_BYPASS=1) must be no-ops
        in every hook — no state writes, no denies, no spawned mines."""
        env = {"AIDE_JUDGE_BYPASS": "1"}
        cases = [
            (POSTTOOL, self.payload(tool_name="WebFetch",
                                    tool_input={"url": "https://x.com"},
                                    hook_event_name="PostToolUse")),
            (PRETOOL, self.payload(tool_name="WebFetch",
                                   tool_input={"url": "https://x.com"})),
            (SESSION_START, self.payload(source="resume")),
            (PRECOMPACT, self.payload(trigger="auto")),
            (STOP_VERIFY, self.payload()),
            (SCHEDULED_MINE, self.payload(source="startup")),
        ]
        for script, payload in cases:
            code, out = run_hook(script, payload, env=env, data_dir=self.data_dir)
            self.assertEqual(code, 0, script.name)
            self.assertIsNone(out, script.name)
        self.assertFalse((self.data_dir / "session_state" / "v2-session.json").exists())

    def test_posttool_increments_web_chain(self):
        for _ in range(3):
            run_hook(POSTTOOL, self.payload(
                tool_name="WebFetch", tool_input={"url": "https://x.com"},
                hook_event_name="PostToolUse"), data_dir=self.data_dir)
        state_path = self.data_dir / "session_state" / "v2-session.json"
        state = json.loads(state_path.read_text())
        self.assertEqual(state["web_chain"], 3)

    def test_pretool_denies_long_web_chain(self):
        state_dir = self.data_dir / "session_state"
        state_dir.mkdir(parents=True)
        (state_dir / "v2-session.json").write_text(json.dumps({
            "web_chain": 12, "research_intent": False,
        }))
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {"web_chain_min": 12},
        }))
        code, out = run_hook(PRETOOL, self.payload(
            tool_name="WebFetch", tool_input={"url": "https://z.com"}),
            data_dir=self.data_dir)
        self.assertEqual(code, 0)
        decision = ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")
        self.assertEqual(decision, "deny")

    def test_pretool_denies_retry_spiral(self):
        import hashlib
        cmd = "npm test"
        h = hashlib.sha1(cmd.encode()).hexdigest()[:12]
        state_dir = self.data_dir / "session_state"
        state_dir.mkdir(parents=True)
        (state_dir / "v2-session.json").write_text(json.dumps({
            "bash": {h: {"count": 3, "stderr_sig": "abc", "cmd": cmd}},
        }))
        code, out = run_hook(PRETOOL, self.payload(
            tool_name="Bash", tool_input={"command": cmd}),
            data_dir=self.data_dir)
        self.assertEqual(((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"),
                         "deny")

    def test_session_start_r11_dedup(self):
        entries = [
            {"type": "user", "timestamp": "2026-07-01T08:00:00Z",
             "message": {"content": "start"}},
            {"type": "assistant", "timestamp": "2026-07-01T08:01:00Z",
             "message": {"usage": {"cache_read_input_tokens": 30000}}},
        ]
        write_transcript(self.transcript, entries)
        (self.data_dir / "rulebook.json").write_text(json.dumps({
            "thresholds": {"stale_hours": 1, "stale_carried_tokens": 10000},
        }))
        code, out = run_hook(SESSION_START, self.payload(), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIn("systemMessage", out or {})
        marks = json.loads((self.data_dir / "session_marks.json").read_text())
        self.assertTrue(marks["v2-session"].get("r11_delivered"))

    def test_precompact_writes_snapshot(self):
        write_transcript(self.transcript, [
            {"type": "user", "message": {"content": "implement feature X"}},
        ])
        code, out = run_hook(PRECOMPACT, self.payload(), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        snaps = list((self.data_dir / "compact-memory").glob("compact-*.md"))
        self.assertEqual(len(snaps), 1)
        self.assertIn("systemMessage", out or {})

    def test_stop_verify_fires_once(self):
        write_transcript(self.transcript, [
            {"type": "user", "message": {"content": "fix auth"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
            ]}},
        ])
        code1, out1 = run_hook(STOP_VERIFY, self.payload(), data_dir=self.data_dir)
        code2, out2 = run_hook(STOP_VERIFY, self.payload(), data_dir=self.data_dir)
        self.assertIn("systemMessage", out1 or {})
        self.assertIsNone(out2)

    def test_stop_verify_once_per_session_after_prompt(self):
        write_transcript(self.transcript, [
            {"type": "user", "message": {"content": "fix auth"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
            ]}},
        ])
        run_hook(STOP_VERIFY, self.payload(), data_dir=self.data_dir)
        run_judge({
            "prompt": "keep going on the auth fix",
            "session_id": "v2-session",
            "cwd": str(self.root),
            "transcript_path": str(self.transcript),
        }, data_dir=self.data_dir)
        code, out = run_hook(STOP_VERIFY, self.payload(), data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_stop_verify_skips_when_stop_hook_active(self):
        code, out = run_hook(STOP_VERIFY, self.payload(stop_hook_active=True),
                             data_dir=self.data_dir)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_scheduled_mine_skips_when_fresh(self):
        import scheduled_mine as sm
        (self.data_dir / "latest.json").write_text(json.dumps({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": "fresh-run",
        }))
        with unittest.mock.patch.object(sm, "LATEST_JSON", self.data_dir / "latest.json"), \
             unittest.mock.patch.object(sm, "LOCK_PATH", self.data_dir / ".mine.lock"), \
             unittest.mock.patch.object(sm, "ROOT", self.data_dir):
            run, reason = sm.should_run_mine(interval_hours=24)
        self.assertFalse(run)
        self.assertIn("fresh_", reason)

    def test_scheduled_mine_runs_when_stale(self):
        import scheduled_mine as sm
        old = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - 48 * 3600))
        (self.data_dir / "latest.json").write_text(json.dumps({
            "generated_at": old,
            "run_id": "old-run",
        }))
        with unittest.mock.patch.object(sm, "LATEST_JSON", self.data_dir / "latest.json"), \
             unittest.mock.patch.object(sm, "LOCK_PATH", self.data_dir / ".mine.lock"), \
             unittest.mock.patch.object(sm, "ROOT", self.data_dir):
            run, reason = sm.should_run_mine(interval_hours=24)
        self.assertTrue(run)
        self.assertIn("stale_", reason)

    def test_scheduled_mine_skips_when_locked(self):
        import scheduled_mine as sm
        (self.data_dir / ".mine.lock").write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": time.time(),
        }))
        with unittest.mock.patch.object(sm, "LATEST_JSON", self.data_dir / "latest.json"), \
             unittest.mock.patch.object(sm, "LOCK_PATH", self.data_dir / ".mine.lock"), \
             unittest.mock.patch.object(sm, "ROOT", self.data_dir):
            self.assertTrue(sm.lock_blocks())
            run, reason = sm.should_run_mine(interval_hours=0)
        self.assertFalse(run)
        self.assertEqual(reason, "locked")

    def test_scheduled_mine_hook_spawns_on_stale(self):
        import scheduled_mine as sm
        old = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - 48 * 3600))
        (self.data_dir / "latest.json").write_text(json.dumps({
            "generated_at": old,
            "run_id": "old-run",
        }))
        env = {
            "AIDE_MINE_DRY_RUN": "1",
            "CLAUDE_JUDGE_HOME": str(self.data_dir),
        }
        with unittest.mock.patch.object(sm, "LATEST_JSON", self.data_dir / "latest.json"), \
             unittest.mock.patch.object(sm, "LOCK_PATH", self.data_dir / ".mine.lock"), \
             unittest.mock.patch.object(sm, "ROOT", self.data_dir), \
             unittest.mock.patch.object(sm, "LOG_PATH", self.data_dir / "scheduled_mine.log"):
            code, out = run_hook(SCHEDULED_MINE, self.payload(), env=env)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out.get("systemMessage", ""))


if __name__ == "__main__":
    unittest.main()

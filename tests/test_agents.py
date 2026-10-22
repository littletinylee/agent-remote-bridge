"""Adapter tests: real hook payload shapes -> normalised events."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from helpers import ArbTestCase  # noqa: F401  (ensures src on path)

from arb import agents


class TestClaudeAdapter(unittest.TestCase):
    def test_user_prompt_submit_is_a_start_event(self) -> None:
        ev = agents.claude_event(
            {"hook_event_name": "UserPromptSubmit", "session_id": "abc", "cwd": "D:/work/proj", "prompt": "修复登录 bug"}
        )
        self.assertEqual((ev.agent, ev.kind, ev.project), ("claude-code", "start", "proj"))
        self.assertIn("修复登录 bug", ev.body)
        self.assertEqual(ev.key, "abc")

    def test_notification_is_attention(self) -> None:
        ev = agents.claude_event({"hook_event_name": "Notification", "message": "Claude needs your permission"})
        self.assertEqual(ev.kind, "attention")
        self.assertIn("permission", ev.body)

    def test_pre_tool_use_describes_the_bash_command(self) -> None:
        ev = agents.claude_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf dist"},
                "session_id": "s1",
            }
        )
        self.assertEqual(ev.kind, "approval")
        self.assertIn("rm -rf dist", ev.body)
        self.assertEqual(agents.approval_prompt(ev), f"{ev.title}\n{ev.body}")

    def test_stop_reads_last_assistant_message_from_transcript(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            fh.write(
                json.dumps(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "已完成重构，测试全部通过"}]}}
                )
                + "\n"
            )
            path = fh.name
        try:
            ev = agents.claude_event({"hook_event_name": "Stop", "transcript_path": path})
            self.assertEqual(ev.kind, "done")
            self.assertIn("测试全部通过", ev.body)
        finally:
            os.unlink(path)

    def test_write_tool_input_rendering(self) -> None:
        ev = agents.claude_event(
            {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x" * 50}}
        )
        self.assertIn("a.py", ev.body)
        self.assertIn("50 字符", ev.body)

    def test_env_overrides_project_and_label(self) -> None:
        os.environ["ARB_PROJECT"] = "custom-proj"
        os.environ["ARB_SESSION_LABEL"] = "子任务A"
        try:
            ev = agents.claude_event({"hook_event_name": "SessionStart", "cwd": "D:/x/ignored"})
            self.assertEqual(ev.project, "custom-proj")
            self.assertEqual(ev.label, "子任务A")
        finally:
            os.environ.pop("ARB_PROJECT", None)
            os.environ.pop("ARB_SESSION_LABEL", None)


class TestCodexAdapter(unittest.TestCase):
    def test_turn_complete_event(self) -> None:
        ev = agents.codex_event(
            {
                "type": "agent-turn-complete",
                "turn-id": "t-1",
                "thread-id": "th-9",
                "last-assistant-message": "重构完成，3 个测试通过",
                "cwd": "D:/repo/svc",
            }
        )
        self.assertEqual(ev.agent, "codex")
        self.assertEqual(ev.kind, "done")
        self.assertEqual(ev.project, "svc")
        self.assertEqual(ev.key, "th-9")
        self.assertIn("重构完成", ev.body)

    def test_unknown_type_is_informational(self) -> None:
        ev = agents.codex_event({"type": "something-else"})
        self.assertEqual(ev.kind, "info")

    def test_sniffing(self) -> None:
        self.assertEqual(agents.sniff_agent({"hook_event_name": "Stop"}), "claude-code")
        self.assertEqual(agents.sniff_agent({"type": "agent-turn-complete"}), "codex")
        os.environ["ARB_AGENT"] = "dsh"
        try:
            self.assertEqual(agents.sniff_agent({"type": "x"}), "dsh")
        finally:
            os.environ.pop("ARB_AGENT", None)


if __name__ == "__main__":
    unittest.main()

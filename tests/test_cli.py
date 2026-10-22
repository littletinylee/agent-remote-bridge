"""CLI-level integration: the real `python -m arb ...` entry points, with a real
daemon, over real HTTP. Catches packaging/argparse/env bugs the unit tests miss.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from helpers import ArbTestCase

from arb import text as T
from arb.channel.console import MemoryChannel
from arb.client import Client
from arb.daemon import Daemon
from arb.store import Store


class TestArgCode(unittest.TestCase):
    def test_forms(self) -> None:
        # codes come from the unambiguous alphabet: no 0/O, 1/I/L
        self.assertEqual(T.arg_code("#AB34 1"), ("AB34", "1"))
        self.assertEqual(T.arg_code("AB34 1"), ("AB34", "1"))
        self.assertEqual(T.arg_code("ab34:1"), ("AB34", "1"))
        self.assertEqual(T.arg_code("#AB34"), ("AB34", ""))
        self.assertEqual(T.arg_code("hello"), (None, "hello"))
        self.assertEqual(T.arg_code("#AB12"), (None, "#AB12"))  # 1 is not in the alphabet

    def test_extract_code_prefers_hash(self) -> None:
        self.assertEqual(T.extract_code("请回答 #K7MQ 允许"), ("K7MQ", "请回答  允许"))
        self.assertEqual(T.extract_code("没有编号"), (None, "没有编号"))


class TestCliSurface(ArbTestCase):
    def run_cli(self, *args: str, stdin: str = "", timeout: float = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["ARB_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "arb", *args],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    def test_version(self) -> None:
        proc = self.run_cli("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agent-remote-bridge", proc.stdout)

    def test_init_is_idempotent(self) -> None:
        proc = self.run_cli("init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.home / "config.json").exists())
        code = proc.stdout.split("绑定码:")[1].splitlines()[0].strip()
        self.assertTrue(code)
        again = self.run_cli("init")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(again.stdout.split("绑定码:")[1].splitlines()[0].strip(), code)

    def test_status_without_daemon(self) -> None:
        proc = self.run_cli("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("守护进程", proc.stdout)

    def test_bom_config_is_tolerated(self) -> None:
        cfg_path = self.home / "config.json"
        cfg_path.write_text("\ufeff" + cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        proc = self.run_cli("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("BOM", proc.stderr)


class TestCliHooksAgainstRealDaemon(ArbTestCase):
    """Runs `arb hook ...` as a separate process, exactly like the agents do."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.owner_ids = ["owner"]
        self.cfg.notify_min_interval_s = 0
        self.cfg.save()
        self.store = Store(self.cfg.db_path)
        self.channel = MemoryChannel()
        self.daemon = Daemon(self.cfg, channel=self.channel, store=self.store)
        self.httpd = self.daemon.start_http()
        self.cfg.port = self.httpd.server_address[1]
        self.cfg.save()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.daemon.start()
        self.client = Client(self.cfg)
        self.client.ensure(spawn=False)

    def tearDown(self) -> None:
        self.daemon.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        super().tearDown()

    def run_cli(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["ARB_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run(
            [sys.executable, "-m", "arb", *args],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )

    def test_codex_notify_payload_arrives_as_argv(self) -> None:
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "th-42",
            "turn-id": "t-1",
            "cwd": "D:/projects/svc",
            "last-assistant-message": "refactor done",
        }
        proc = self.run_cli("hook", "codex", json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            self.channel.wait_sent(1, timeout=8),
            f"notification should be pushed; hook out={proc.stdout!r} err={proc.stderr!r}",
        )
        self.assertIn("Codex", self.channel.last_text())
        self.assertIn("refactor done", self.channel.last_text())
        sessions = self.store.list_sessions()
        self.assertTrue(any(s["agent"] == "codex" and s["project"] == "svc" for s in sessions), sessions)

    def test_claude_hook_payload_arrives_on_stdin(self) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-sess-9",
            "cwd": "D:/projects/webapp",
            "prompt": "修复登录页报错",
        }
        proc = self.run_cli("hook", "claude-code", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.channel.wait_sent(1, timeout=8))
        self.assertIn("开始新任务", self.channel.last_text())
        sessions = self.store.list_sessions()
        self.assertTrue(any(s["agent"] == "claude-code" and s["session_key"] == "claude-sess-9" for s in sessions))

    def test_pre_tool_use_is_ignored_when_approval_mode_is_notify(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/x"},
            "cwd": "D:/p",
        }
        proc = self.run_cli("hook", "claude-code", stdin=json.dumps(payload))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # no question must be created, and nothing may be auto-approved
        self.assertEqual(self.store.pending_questions(), [])
        self.assertEqual(proc.stdout.strip(), "")

    def test_garbage_stdin_does_not_crash_the_hook(self) -> None:
        proc = self.run_cli("hook", "codex", stdin="not json at all")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()

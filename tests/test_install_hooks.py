"""Hook installer must be additive, idempotent and reversible."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import ArbTestCase

from arb import install_hooks


class TestClaudeHookInstaller(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = self.home / "claude" / "settings.json"
        self.settings.parent.mkdir(parents=True, exist_ok=True)

    def write(self, data: dict) -> None:
        self.settings.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_into_empty_settings(self) -> None:
        res = install_hooks.install(self.settings, self.home, sys_exe())
        self.assertTrue(res["changed"])
        data = self.read()
        for event in install_hooks.CLAUDE_EVENTS:
            self.assertIn(event, data["hooks"])
        pre = data["hooks"]["PreToolUse"][0]
        self.assertEqual(pre["matcher"], "Bash|Write|Edit|MultiEdit|NotebookEdit")
        self.assertIn("arb-hook", pre["hooks"][0]["command"])
        self.assertEqual(pre["hooks"][0]["timeout"], 900)

    def test_install_is_idempotent(self) -> None:
        install_hooks.install(self.settings, self.home, sys_exe())
        before = self.read()
        res = install_hooks.install(self.settings, self.home, sys_exe())
        self.assertFalse(res["changed"])
        self.assertEqual(self.read(), before)

    def test_preserves_foreign_hooks(self) -> None:
        self.write(
            {
                "model": "opus",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                    "PreToolUse": [
                        {"matcher": "Read", "hooks": [{"type": "command", "command": "echo read-guard"}]}
                    ],
                },
            }
        )
        install_hooks.install(self.settings, self.home, sys_exe())
        data = self.read()
        stop_cmds = [e["command"] for b in data["hooks"]["Stop"] for e in b["hooks"]]
        self.assertIn("echo mine", stop_cmds)
        pre_matchers = [b.get("matcher") for b in data["hooks"]["PreToolUse"]]
        self.assertIn("Read", pre_matchers)
        self.assertIn("Bash|Write|Edit|MultiEdit|NotebookEdit", pre_matchers)
        self.assertEqual(data["model"], "opus", "unrelated settings must be kept")

    def test_uninstall_removes_only_ours(self) -> None:
        self.write({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}})
        install_hooks.install(self.settings, self.home, sys_exe())
        res = install_hooks.uninstall(self.settings)
        self.assertTrue(res["changed"])
        data = self.read()
        stop_cmds = [e["command"] for b in data["hooks"].get("Stop", []) for e in b["hooks"]]
        self.assertEqual(stop_cmds, ["echo mine"])
        self.assertNotIn("SessionStart", data["hooks"])

    def test_dry_run_changes_nothing(self) -> None:
        res = install_hooks.install(self.settings, self.home, sys_exe(), dry_run=True)
        self.assertTrue(res["changed"])
        self.assertFalse(self.settings.exists())

    def test_status_reports_events(self) -> None:
        self.assertFalse(install_hooks.status(self.settings)["installed"])
        install_hooks.install(self.settings, self.home, sys_exe())
        status = install_hooks.status(self.settings)
        self.assertTrue(status["installed"])
        self.assertIn("PreToolUse", [e["event"] for e in status["events"]])

    def test_backup_is_kept(self) -> None:
        self.write({"model": "opus"})
        res = install_hooks.install(self.settings, self.home, sys_exe())
        self.assertTrue(res["backup"])
        self.assertTrue(Path(res["backup"]).exists())
        self.assertIn("opus", Path(res["backup"]).read_text(encoding="utf-8"))

    def test_bom_settings_survive(self) -> None:
        self.settings.write_text('\ufeff{"model": "opus"}', encoding="utf-8")
        install_hooks.install(self.settings, self.home, sys_exe())
        self.assertEqual(self.read()["model"], "opus")


def sys_exe() -> str:
    import sys

    return sys.executable


if __name__ == "__main__":
    unittest.main()

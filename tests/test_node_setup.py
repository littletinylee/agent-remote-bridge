"""`arb node setup` writes agent-native plugin wiring; keep it idempotent."""

from __future__ import annotations

from pathlib import Path

from helpers import ArbTestCase

from arb import node_setup


class TestCodexPlugin(ArbTestCase):
    def test_marketplace_and_plugin_are_valid_json(self) -> None:
        import json

        files = node_setup.codex_files(self.home, "C:/py/python.exe", self.home)
        paths = {str(p.relative_to(self.home)).replace("\\", "/"): json.loads(t) for p, t in files.items()}
        assert paths["codex-marketplace/marketplace.json"]["name"] == node_setup.MARKET_NAME
        plugin = paths["codex-marketplace/plugins/agent-remote-bridge/.codex-plugin/plugin.json"]
        self.assertEqual(plugin["name"], node_setup.PLUGIN_NAME)
        self.assertEqual(plugin["mcpServers"], "./.mcp.json")
        hooks = plugin["hooks"]["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "PreToolUse"):
            self.assertIn(event, hooks)
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash|Write|Edit|MultiEdit")
        self.assertIn("arb-hook-codex.cmd", hooks["Stop"][0]["hooks"][0]["command"])
        mcp = paths["codex-marketplace/plugins/agent-remote-bridge/.mcp.json"]["mcpServers"]["arb"]
        self.assertEqual(mcp["args"][:4], ["-m", "arb", "mcp", "--agent"])

    def test_hook_command_is_a_single_executable_path(self) -> None:
        """Codex only starts hooks whose command is one path (measured)."""
        import json

        files = node_setup.codex_files(self.home, "C:/py/python.exe", self.home)
        plugin = json.loads(
            files[self.home / "codex-marketplace" / "plugins" / "agent-remote-bridge" / ".codex-plugin" / "plugin.json"]
        )
        for blocks in plugin["hooks"]["hooks"].values():
            for block in blocks:
                for hook in block["hooks"]:
                    self.assertEqual(hook["type"], "command")
                    cmd = hook["command"]
                    self.assertTrue(cmd.startswith('"') and cmd.endswith('"'), cmd)
                    self.assertNotIn(" -m ", cmd)


class TestDshEntry(ArbTestCase):
    def test_install_is_idempotent_and_backs_up(self) -> None:
        profile = self.home / "cordis.patch.yml"
        profile.write_text("[]\n", encoding="utf-8")
        first = node_setup.install_dsh(self.home, "C:/py/python.exe", profile)
        self.assertTrue(first["changed"])
        text = profile.read_text(encoding="utf-8")
        self.assertIn("id: mcp-arb", text)
        self.assertIn("@deepseek-ai/dsh-mcp-client", text)
        self.assertIn("toolCallTimeoutMs: 900000", text)
        self.assertTrue(Path(first["backup"]).exists())

        second = node_setup.install_dsh(self.home, "C:/py/python.exe", profile)
        self.assertFalse(second["changed"])
        self.assertEqual(profile.read_text(encoding="utf-8"), text, "must not duplicate the entry")

    def test_uninstall_removes_only_our_entry(self) -> None:
        profile = self.home / "cordis.patch.yml"
        profile.write_text(
            "- id: keep-me\n  name: '@deepseek-ai/dsh-agent-default-model'\n  config:\n    model: x\n",
            encoding="utf-8",
        )
        node_setup.install_dsh(self.home, "C:/py/python.exe", profile)
        res = node_setup.uninstall_dsh(profile)
        self.assertTrue(res["changed"])
        text = profile.read_text(encoding="utf-8")
        self.assertIn("keep-me", text)
        self.assertNotIn("mcp-arb", text)

    def test_missing_profile_is_reported_not_created(self) -> None:
        res = node_setup.install_dsh(self.home, "C:/py/python.exe", self.home / "nope.yml")
        self.assertFalse(res["changed"])
        self.assertIn("not found", res["error"])


class TestWrappers(ArbTestCase):
    def test_wrappers_bake_in_pythonpath_and_forward_args(self) -> None:
        out = node_setup.write_wrappers(self.home, "C:/py/python.exe")
        hook_codex = out["hook_codex"].read_text(encoding="ascii")
        self.assertIn("PYTHONPATH=", hook_codex)
        self.assertIn("-m arb hook codex %*", hook_codex)
        self.assertIn("-m arb mcp %*", out["mcp"].read_text(encoding="ascii"))

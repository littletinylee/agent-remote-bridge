"""M3 advisor: read-only, never an approver, rate limited."""

from __future__ import annotations

import json
import unittest

from helpers import ArbTestCase

from arb.hub import HubStore
from arb.hub_chat import SYSTEM_PROMPT, Advisor


class TestAdvisor(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.home / "hub.sqlite3")
        self.store.register("workbench", "t1")
        self.store.register("laptop2", "t2")

    def test_context_lists_machines_and_pending(self) -> None:
        adv = Advisor("k", self.store)
        ctx = adv.context()
        self.assertIn("workbench", ctx)
        self.assertIn("laptop2", ctx)
        self.assertIn("待决策: 无", ctx)

    def test_system_prompt_forbids_approval(self) -> None:
        self.assertIn("没有任何审批权限", SYSTEM_PROMPT)
        self.assertIn("不能编造", SYSTEM_PROMPT)

    def test_rate_limit(self) -> None:
        adv = Advisor("k", self.store, max_per_minute=2)
        self.assertTrue(adv._allow())
        self.assertTrue(adv._allow())
        self.assertFalse(adv._allow())
        self.assertIn("限流", adv.reply("你好", []) or "")

    def test_llm_payload_and_reply(self) -> None:
        adv = Advisor("k", self.store)
        seen: dict = {}

        def fake(messages):
            seen["messages"] = messages
            return "现在 2 台机器在线。"

        adv._complete = fake  # type: ignore[method-assign]
        out = adv.reply("现在几台机器？", ["workbench"])
        self.assertEqual(out, "现在 2 台机器在线。")
        self.assertEqual(seen["messages"][0]["role"], "system")
        self.assertIn("当前机器状态", seen["messages"][1]["content"])
        self.assertIn("workbench", seen["messages"][1]["content"])

    def test_llm_failure_degrades_and_audits(self) -> None:
        adv = Advisor("k", self.store)

        def boom(messages):
            raise RuntimeError("no network")

        adv._complete = boom  # type: ignore[method-assign]
        out = adv.reply("在吗", [])
        self.assertIn("不可用", out or "")
        audits = [r["action"] for r in self.store.q("SELECT action FROM audit")]
        self.assertIn("llm_error", audits)

    def test_reply_never_claims_to_have_executed_anything(self) -> None:
        """The prompt is the contract; assert it is actually sent every time."""
        adv = Advisor("k", self.store)
        sent: list[dict] = []

        def fake(messages):
            sent.append(messages[0])
            return "ok"

        adv._complete = fake  # type: ignore[method-assign]
        adv.reply("帮我把测试跑一下", [])
        self.assertIn("不要声称你执行了任何命令", sent[0]["content"])

    def test_context_includes_snapshot_summary(self) -> None:
        node = self.store.node_by_name("workbench")
        self.store.x(
            "UPDATE nodes SET snapshot=?, projects=? WHERE id=?",
            (json.dumps({"summary": "分支 main，2 个文件未提交"}), json.dumps(["repo-a"]), node["id"]),
        )
        ctx = Advisor("k", self.store).context()
        self.assertIn("2 个文件未提交", ctx)
        self.assertIn("repo-a", ctx)


if __name__ == "__main__":
    unittest.main()

"""A failed WeChat send must be visible, and must let the node fall back.

Production failure: the hub's bot session went stale, iLink answered
{"ret": -2, "errmsg": "prepare failed"}, say() ignored it (it only looked at
errcode == -14) and returned a message id - so the human got nothing, the node
believed the push happened, and nothing retried.
"""

from __future__ import annotations

import unittest

from helpers import ArbTestCase

from arb.hub import Hub, HubStore


class RefusingWeChat:
    token = "t"

    def __init__(self, response: dict):
        self.response = response
        self.sent: list[str] = []

    def send_message(self, to_user: str, text: str, context_token: str = "") -> dict:
        self.sent.append(text)
        return self.response


class TestFailedSend(ArbTestCase):
    def make(self, response: dict):
        store = HubStore(self.home / "hub.sqlite3")
        wx = RefusingWeChat(response)
        hub = Hub(store, wx, ["owner"])  # type: ignore[arg-type]
        return store, hub, wx

    def test_prepare_failed_is_not_treated_as_sent(self) -> None:
        store, hub, wx = self.make({"ret": -2, "errmsg": "prepare failed"})
        self.assertEqual(hub.say("hello"), "", "must not pretend a message id")
        actions = [r["action"] for r in store.q("SELECT action FROM audit")]
        self.assertIn("send_failed", actions)
        self.assertTrue(wx.sent, "it did attempt the send")

    def test_token_expired_is_audited_separately(self) -> None:
        store, hub, _ = self.make({"ret": -14, "errmsg": "token"})
        self.assertEqual(hub.say("hello"), "")
        actions = [r["action"] for r in store.q("SELECT action FROM audit")]
        self.assertIn("token_expired", actions)

    def test_question_reports_failure_so_the_node_can_fallback(self) -> None:
        store, hub, _ = self.make({"ret": -2, "errmsg": "prepare failed"})
        node = store.register("workbench", "t1")
        ok = hub.question_from_node(node["node_id"], "AB34", "要不要删库")
        self.assertFalse(ok, "the caller must learn the push failed")
        self.assertIsNotNone(store.resolve("AB34"), "the code is still indexed")
        actions = [r["action"] for r in store.q("SELECT action FROM audit")]
        self.assertIn("question_push_failed", actions)

    def test_successful_send_returns_the_message_id(self) -> None:
        store, hub, _ = self.make({"ret": 0, "message_id": 42})
        self.assertEqual(hub.say("hello"), "42")
        node = store.register("workbench", "t1")
        self.assertTrue(hub.question_from_node(node["node_id"], "AB34", "q"))
        self.assertEqual(store.resolve("AB34")["msg_id"], "42")


if __name__ == "__main__":
    unittest.main()

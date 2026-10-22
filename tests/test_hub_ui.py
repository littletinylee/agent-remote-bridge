"""Web console: token required, and it must behave exactly like a WeChat reply."""

from __future__ import annotations

import unittest

from helpers import ArbTestCase

from arb.hub import HubStore
from arb.hub_ui import handle_ui


class FakeHub:
    ui_token = "TOK"
    owner_ids = ["wxid_owner"]


class TestWebConsole(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.home / "hub.sqlite3")
        self.node = self.store.register("workbench", "t1")
        self.store.put_question("AB34", self.node["node_id"], "要不要删库?")
        self.hub = FakeHub()

    def test_token_is_required(self) -> None:
        self.assertEqual(handle_ui(self.store, self.hub, "GET", {}, {})[0], 403)
        self.assertEqual(handle_ui(self.store, self.hub, "GET", {"token": "nope"}, {})[0], 403)
        self.assertEqual(handle_ui(self.store, self.hub, "POST", {}, {"code": "AB34", "answer": "yes"})[0], 403)

    def test_disabled_without_ui_token(self) -> None:
        class Off:
            ui_token = ""
            owner_ids = ["o"]

        self.assertEqual(handle_ui(self.store, Off(), "GET", {"token": "x"}, {})[0], 404)

    def test_lists_pending_with_its_machine(self) -> None:
        status, html = handle_ui(self.store, self.hub, "GET", {"token": "TOK"}, {})
        self.assertEqual(status, 200)
        self.assertIn("AB34", html)
        self.assertIn("workbench", html)
        self.assertIn("要不要删库", html)

    def test_answer_goes_to_the_node_as_the_owner(self) -> None:
        status, _ = handle_ui(self.store, self.hub, "POST", {}, {"token": "TOK", "code": "AB34", "answer": "yes"})
        self.assertEqual(status, 200)
        msgs = self.store.take(self.node["node_id"], 0)
        self.assertEqual(msgs[0]["kind"], "answer")
        self.assertEqual(msgs[0]["code"], "AB34")
        self.assertEqual(msgs[0]["text"], "yes")
        self.assertEqual(msgs[0]["frm"], "wxid_owner", "the node re-checks this against its allowlist")

    def test_web_answer_does_not_close_the_question(self) -> None:
        handle_ui(self.store, self.hub, "POST", {}, {"token": "TOK", "code": "AB34", "answer": "yes"})
        self.assertEqual(self.store.resolve("AB34")["state"], "pending", "only the node may close it")

    def test_unknown_code_is_refused(self) -> None:
        status, _ = handle_ui(self.store, self.hub, "POST", {}, {"token": "TOK", "code": "ZZZZ", "answer": "yes"})
        self.assertEqual(status, 404)

    def test_answer_to_a_closed_question_is_refused(self) -> None:
        self.store.close_question("AB34")
        status, _ = handle_ui(self.store, self.hub, "POST", {}, {"token": "TOK", "code": "AB34", "answer": "yes"})
        self.assertEqual(status, 409)
        self.assertEqual(self.store.take(self.node["node_id"], 0), [])


if __name__ == "__main__":
    unittest.main()

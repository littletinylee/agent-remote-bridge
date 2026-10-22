"""Web console: login, cookie, and the same answer path as WeChat."""

from __future__ import annotations

import unittest

from helpers import ArbTestCase

from arb.hub import HubStore
from arb.hub_ui import handle_ui


class FakeHub:
    ui_user = "q045204"
    ui_password = "q045204"
    owner_ids = ["wxid_owner"]


SESSION_MARK = "arb_ui="


class TestWebConsole(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.home / "hub.sqlite3")
        self.node = self.store.register("workbench", "t1")
        self.store.put_question("AB34", self.node["node_id"], "要不要删库?")
        self.hub = FakeHub()

    def login(self):
        return handle_ui(self.store, self.hub, "POST", {}, {"user": "q045204", "password": "q045204"})

    def cookie_of(self) -> str:
        _, _, cookie = self.login()
        return cookie.split(";")[0].split("=", 1)[1]

    def test_without_login_you_get_a_form_and_no_question_text(self) -> None:
        status, html_text, cookie = handle_ui(self.store, self.hub, "GET", {}, {})
        self.assertEqual(status, 200)
        self.assertIn("密码", html_text)
        self.assertNotIn("AB34", html_text, "nothing may leak before login")
        self.assertEqual(cookie, "")

    def test_wrong_password_is_refused(self) -> None:
        status, _, cookie = handle_ui(self.store, self.hub, "POST", {}, {"user": "q045204", "password": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(cookie, "")

    def test_correct_password_logs_in_and_sets_a_cookie(self) -> None:
        status, html_text, cookie = self.login()
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith(SESSION_MARK))
        self.assertIn("AB34", html_text)
        self.assertNotIn("q045204", cookie, "the cookie must not be the password")

    def test_cookie_keeps_you_logged_in(self) -> None:
        value = self.cookie_of()
        status, html_text, _ = handle_ui(self.store, self.hub, "GET", {}, {}, value)
        self.assertEqual(status, 200)
        self.assertIn("AB34", html_text)

    def test_answer_goes_to_the_node_as_the_owner(self) -> None:
        value = self.cookie_of()
        status, _, _ = handle_ui(self.store, self.hub, "POST", {}, {"code": "AB34", "answer": "yes"}, value)
        self.assertEqual(status, 200)
        msgs = self.store.take(self.node["node_id"], 0)
        self.assertEqual((msgs[0]["kind"], msgs[0]["code"], msgs[0]["text"]), ("answer", "AB34", "yes"))
        self.assertEqual(msgs[0]["frm"], "wxid_owner", "the node re-checks this against its allowlist")

    def test_web_answer_does_not_close_the_question(self) -> None:
        value = self.cookie_of()
        handle_ui(self.store, self.hub, "POST", {}, {"code": "AB34", "answer": "yes"}, value)
        self.assertEqual(self.store.resolve("AB34")["state"], "pending", "only the node may close it")

    def test_unknown_or_closed_code_is_refused(self) -> None:
        value = self.cookie_of()
        self.assertEqual(
            handle_ui(self.store, self.hub, "POST", {}, {"code": "ZZZZ", "answer": "y"}, value)[0], 404
        )
        self.store.close_question("AB34")
        self.assertEqual(
            handle_ui(self.store, self.hub, "POST", {}, {"code": "AB34", "answer": "y"}, value)[0], 409
        )
        self.assertEqual(self.store.take(self.node["node_id"], 0), [])


if __name__ == "__main__":
    unittest.main()

"""Replay both iLink quote formats through the actual polling and routing paths."""

from __future__ import annotations

import unittest

from helpers import ArbTestCase

from arb.channel.ilink import IlinkChannel
from arb.hub import Hub, HubStore
from arb.router import Router
from arb.store import Store


def quoted_reply(code: str = "CD56", answer: str = "yes", *, ref: dict | None = None) -> dict:
    if ref is None:
        ref = {
            "message_item": {
                "type": 1,
                "text_item": {"text": f"🚨 需要你决策  #{code}\n[machine-b]\nPrompt mentions #AB34"},
            },
            "title": "quoted message",
        }
    return {
        "message_id": "reply-1", "message_type": 1, "from_user_id": "owner",
        "context_token": "ctx-reply",
        "item_list": [{"type": 1, "text_item": {"text": answer}, "ref_msg": ref}],
    }


class TestQuoteParsing(unittest.TestCase):
    def test_embedded_original_header_is_routing_data_only(self) -> None:
        msg = IlinkChannel._parse(quoted_reply())
        self.assertEqual(msg.text, "yes")
        self.assertEqual(msg.ref_code, "CD56")
        self.assertEqual(msg.ref_svr_id, "")
        self.assertTrue(msg.quoted)

    def test_server_id_is_preserved_without_integer_precision_loss(self) -> None:
        msg = IlinkChannel._parse(quoted_reply(ref={"svr_id": 7518925618736178056}))
        self.assertEqual(msg.ref_svr_id, "7518925618736178056")
        self.assertTrue(msg.quoted)

    def test_arbitrary_code_in_quoted_notification_is_not_a_question(self) -> None:
        msg = IlinkChannel._parse(quoted_reply(ref={
            "message_item": {"type": 1, "text_item": {"text": "Completed #CD56"}},
            "title": "#CD56",
        }))
        self.assertTrue(msg.quoted)
        self.assertEqual(msg.ref_code, "")

    def test_quote_without_answer_is_not_an_approval(self) -> None:
        self.assertIsNone(IlinkChannel._parse(quoted_reply(answer="")))


class TestHubQuotePolling(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.home / "hub.sqlite3")
        self.a = self.store.register("machine-a", "a")["node_id"]
        self.b = self.store.register("machine-b", "b")["node_id"]
        self.store.put_question("AB34", self.a, "A", "server-a")
        self.store.put_question("CD56", self.b, "B", "server-b")
        outer = self

        class Client:
            def get_updates(self, buf: str, timeout: float) -> dict:
                outer.hub.stop()
                return {"msgs": [outer.message]}

            def send_message(self, *args, **kwargs) -> dict:
                return {"message_id": "notice"}

        self.hub = Hub(self.store, Client(), ["owner"])  # type: ignore[arg-type]

    def poll(self, message: dict) -> list[dict]:
        self.message = message
        self.hub.run_wechat()
        return [dict(r) for r in self.store.q("SELECT * FROM inbox")]

    def test_embedded_quote_reaches_exact_node_with_two_pending_questions(self) -> None:
        rows = self.poll(quoted_reply())
        self.assertEqual([(r["node_id"], r["code"], r["text"], r["frm"]) for r in rows],
                         [(self.b, "CD56", "yes", "owner")])
        self.assertEqual(self.store.resolve("CD56")["state"], "pending")
        audit = self.store.q1("SELECT detail FROM audit WHERE action='answer_forwarded'")
        self.assertIn('"source":"quote"', audit["detail"].replace(" ", ""))
        self.assertEqual(self.store.kv_get("ctx.owner"), "ctx-reply")

    def test_server_id_quote_still_works(self) -> None:
        rows = self.poll(quoted_reply(ref={"svr_id": "server-a"}))
        self.assertEqual([r["code"] for r in rows], ["AB34"])

    def test_known_server_id_wins_over_embedded_code(self) -> None:
        message = quoted_reply()
        message["item_list"][0]["ref_msg"]["svr_id"] = "server-a"
        self.assertEqual([r["code"] for r in self.poll(message)], ["AB34"])

    def test_unknown_server_id_can_use_original_header(self) -> None:
        message = quoted_reply()
        message["item_list"][0]["ref_msg"]["svr_id"] = "unmapped-id"
        self.assertEqual([r["code"] for r in self.poll(message)], ["CD56"])

    def test_unknown_quote_never_answers_the_only_pending_question(self) -> None:
        self.store.close_question("AB34")
        self.assertEqual(self.poll(quoted_reply(ref={"svr_id": "unknown"})), [])
        self.assertIsNotNone(self.store.q1("SELECT 1 FROM audit WHERE action='quote_unknown'"))

    def test_empty_quote_is_not_downgraded_to_bare_answer(self) -> None:
        self.store.close_question("AB34")
        self.assertEqual(self.poll(quoted_reply(ref={})), [])

    def test_old_embedded_quote_never_answers_a_new_question(self) -> None:
        self.store.close_question("CD56")
        self.assertEqual(self.poll(quoted_reply()), [])

    def test_unauthorized_sender_cannot_answer_by_quote(self) -> None:
        message = quoted_reply()
        message["from_user_id"] = "stranger"
        self.assertEqual(self.poll(message), [])


class TestDirectQuoteRouting(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.owner_ids = ["owner"]
        self.store = Store(self.cfg.db_path)
        self.router = Router(self.cfg, self.store, lambda *args: None)
        self.session = self.store.upsert_session(agent="codex", project="test", cwd="D:/test", key="test")

    def question(self, dangerous: bool = False):
        return self.store.create_question(
            session_id=self.session["id"], prompt="Approve?", options=["允许", "拒绝"], dangerous=dangerous,
        )

    def test_embedded_quote_selects_question_without_using_quoted_text_as_answer(self) -> None:
        a, b = self.question(), self.question()
        msg = IlinkChannel._parse(quoted_reply(b.code))
        self.assertEqual(self.router.handle_inbound(msg), f"answered:{b.code}")
        self.assertEqual(self.store.get_question(a.id).state, "pending")
        self.assertEqual(self.store.get_question(b.id).answer, "允许")

    def test_unknown_quote_does_not_consume_sole_question(self) -> None:
        q = self.question()
        msg = IlinkChannel._parse(quoted_reply(ref={"svr_id": "unknown"}))
        self.assertEqual(self.router.handle_inbound(msg), "unknown_quote")
        self.assertEqual(self.store.get_question(q.id).state, "pending")

    def test_quote_does_not_bypass_dangerous_approval_rule(self) -> None:
        q = self.question(dangerous=True)
        msg = IlinkChannel._parse(quoted_reply(q.code, answer="1"))
        self.assertEqual(self.router.handle_inbound(msg), "dangerous_needs_explicit")
        self.assertEqual(self.store.get_question(q.id).state, "pending")

    def test_closed_quote_does_not_consume_another_question(self) -> None:
        old = self.question()
        self.router.handle_inbound(IlinkChannel._parse(quoted_reply(old.code)))
        new = self.question()
        msg = IlinkChannel._parse(quoted_reply(old.code))
        self.assertEqual(self.router.handle_inbound(msg), "not_pending:answered")
        self.assertEqual(self.store.get_question(new.id).state, "pending")

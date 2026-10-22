"""Routing tests — the anti-crosstalk and approval-safety contract.

These are the tests that matter most: a wrong route sends a code decision to the
wrong agent, an over-eager match approves a destructive command.
"""

from __future__ import annotations

import time
import unittest

from helpers import ArbTestCase

from arb.channel.base import InboundMessage
from arb.config import Config
from arb.router import Router
from arb.store import Store


class RouterHarness(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(self.cfg.db_path)
        self.cfg.owner_ids = ["owner-1"]
        self.cfg.pairing_code = "PAIR1234"
        self.cfg.save()
        self.sent: list[str] = []

        def send(text: str, kind: str = "reply") -> None:
            self.sent.append(text)

        self.router = Router(self.cfg, self.store, send)
        self.sess_a = self.store.upsert_session(agent="claude-code", project="projA", cwd="D:/a", key="a")
        self.sess_b = self.store.upsert_session(agent="codex", project="projB", cwd="D:/b", key="b")

    # helpers
    def inbound(self, text: str, from_user: str = "owner-1", ref: str = "") -> InboundMessage:
        return InboundMessage(msg_id=f"m-{time.time()}-{len(self.sent)}", from_user=from_user, text=text, ref_svr_id=ref)

    def ask_a(self, prompt: str = "A 需要决策", options=("允许", "拒绝"), dangerous=False):
        return self.store.create_question(
            session_id=self.sess_a["id"], prompt=prompt, options=list(options), dangerous=dangerous
        )

    def ask_b(self, prompt: str = "B 需要决策", options=("允许", "拒绝"), dangerous=False):
        return self.store.create_question(
            session_id=self.sess_b["id"], prompt=prompt, options=list(options), dangerous=dangerous
        )


class TestAuthorization(RouterHarness):
    def test_unknown_sender_is_rejected_and_owner_warned(self) -> None:
        q = self.ask_a()
        outcome = self.router.handle_inbound(self.inbound("允许", from_user="stranger"))
        self.assertEqual(outcome, "unauthorized")
        self.assertEqual(self.store.get_question(q.id).state, "pending")
        security = self.store.q("SELECT text FROM outbox WHERE kind='security'")
        self.assertTrue(security, "owner must be warned about an unknown sender")
        # the pairing code must never be sent to the stranger
        self.assertNotIn("PAIR1234", "".join(t for t in self.sent))

    def test_pairing_requires_the_right_code(self) -> None:
        self.cfg.owner_ids = []
        self.assertEqual(self.router.handle_inbound(self.inbound("/pair WRONG", from_user="new"))[:4], "pair")
        self.assertEqual(self.cfg.owner_ids, [])
        self.assertEqual(self.router.handle_inbound(self.inbound("/pair PAIR1234", from_user="new")), "paired")
        self.assertIn("new", self.cfg.owner_ids)
        reloaded = Config.from_dict(__import__("json").loads(self.cfg.config_path.read_text(encoding="utf-8-sig")))
        self.assertIn("new", reloaded.owner_ids, "pairing must persist")
        # the used code must not work a second time
        self.assertNotEqual(reloaded.pairing_code, "PAIR1234", "pairing code must rotate after use")
        before = list(reloaded.owner_ids)
        self.assertEqual(self.router.handle_inbound(self.inbound("/pair PAIR1234", from_user="other")), "pair_failed")
        self.assertEqual(reloaded.owner_ids, before)


class TestSolePending(RouterHarness):
    def test_bare_text_answers_the_only_question(self) -> None:
        q = self.ask_a(options=("允许", "拒绝"))
        self.assertEqual(self.router.handle_inbound(self.inbound("1")), f"answered:{q.code}")
        answered = self.store.get_question(q.id)
        self.assertEqual(answered.state, "answered")
        self.assertEqual(answered.answer, "允许")

    def test_chinese_synonym_maps_to_option(self) -> None:
        q = self.ask_a(options=("允许", "拒绝"))
        self.router.handle_inbound(self.inbound("同意"))
        self.assertEqual(self.store.get_question(q.id).answer, "允许")

    def test_unparseable_reply_does_not_consume(self) -> None:
        q = self.ask_a(options=("允许", "拒绝"))
        self.assertEqual(self.router.handle_inbound(self.inbound("随便吧")), "unparsed")
        self.assertEqual(self.store.get_question(q.id).state, "pending")

    def test_no_pending_replies_helpfully(self) -> None:
        self.assertEqual(self.router.handle_inbound(self.inbound("你好")), "no_pending")
        self.assertTrue(any("没有等待回复" in s for s in self.sent))


class TestAmbiguity(RouterHarness):
    def test_two_pending_refuses_bare_text(self) -> None:
        qa = self.ask_a()
        qb = self.ask_b()
        outcome = self.router.handle_inbound(self.inbound("1"))
        self.assertEqual(outcome, "ambiguous")
        self.assertEqual(self.store.get_question(qa.id).state, "pending")
        self.assertEqual(self.store.get_question(qb.id).state, "pending")
        self.assertTrue(any(qa.code in s and qb.code in s for s in self.sent))

    def test_code_disambiguates(self) -> None:
        qa = self.ask_a()
        qb = self.ask_b()
        self.router.handle_inbound(self.inbound(f"#{qb.code} 2"))
        self.assertEqual(self.store.get_question(qb.id).answer, "拒绝")
        self.assertEqual(self.store.get_question(qa.id).state, "pending", "other session must be untouched")

    def test_quoted_reply_routes_by_message_id(self) -> None:
        qa = self.ask_a()
        qb = self.ask_b()
        self.store.mark_delivered(qb.id, "svr-777")
        self.router.handle_inbound(self.inbound("1", ref="svr-777"))
        self.assertEqual(self.store.get_question(qb.id).answer, "允许")
        self.assertEqual(self.store.get_question(qa.id).state, "pending")

    def test_unknown_code_is_reported(self) -> None:
        self.ask_a()
        self.assertEqual(self.router.handle_inbound(self.inbound("#ZZZZ 1")), "unknown_code")

    def test_code_without_answer_text_asks_for_it(self) -> None:
        q = self.ask_a()
        self.assertEqual(self.router.handle_inbound(self.inbound(f"#{q.code}")), "need_answer_text")
        self.assertEqual(self.store.get_question(q.id).state, "pending")


class TestNoCrosstalk(RouterHarness):
    def test_parallel_decisions_route_independently(self) -> None:
        qa = self.ask_a(prompt="A: 用 Postgres?")
        qb = self.ask_b(prompt="B: 删掉分支?")
        self.router.handle_inbound(self.inbound(f"#{qa.code} 1"))
        self.router.handle_inbound(self.inbound(f"#{qb.code} 2"))
        a = self.store.get_question(qa.id)
        b = self.store.get_question(qb.id)
        self.assertEqual((a.state, a.answer), ("answered", "允许"))
        self.assertEqual((b.state, b.answer), ("answered", "拒绝"))

    def test_second_answer_is_rejected(self) -> None:
        qa = self.ask_a()
        self.assertEqual(self.router.handle_inbound(self.inbound(f"#{qa.code} 1")), f"answered:{qa.code}")
        self.assertEqual(self.router.handle_inbound(self.inbound(f"#{qa.code} 2")), "not_pending:answered")
        self.assertEqual(self.store.get_question(qa.id).answer, "允许", "first answer must win")

    def test_expired_question_reply_is_rejected(self) -> None:
        qa = self.ask_a()
        self.store.x("UPDATE questions SET expires_at=? WHERE id=?", (time.time() - 5, qa.id))
        self.store.expire_due()
        outcome = self.router.handle_inbound(self.inbound(f"#{qa.code} 1"))
        self.assertEqual(outcome, "not_pending:expired")
        self.assertEqual(self.store.get_question(qa.id).state, "expired")


class TestDangerousGate(RouterHarness):
    def test_bare_number_is_not_enough_for_dangerous(self) -> None:
        q = self.ask_a(options=("allow", "deny"), dangerous=True)
        self.assertEqual(self.router.handle_inbound(self.inbound("1")), "dangerous_needs_explicit")
        self.assertEqual(self.store.get_question(q.id).state, "pending")

    def test_explicit_yes_approves(self) -> None:
        q = self.ask_a(options=("allow", "deny"), dangerous=True)
        self.router.handle_inbound(self.inbound("yes"))
        self.assertEqual(self.store.get_question(q.id).answer, "allow")

    def test_approve_command_approves(self) -> None:
        q = self.ask_a(options=("allow", "deny"), dangerous=True)
        self.router.handle_inbound(self.inbound(f"/approve {q.code}"))
        self.assertEqual(self.store.get_question(q.id).answer, "allow")

    def test_deny_command_denies(self) -> None:
        q = self.ask_a(options=("allow", "deny"), dangerous=True)
        self.router.handle_inbound(self.inbound(f"/deny {q.code}"))
        self.assertEqual(self.store.get_question(q.id).answer, "deny")

    def test_dangerous_with_code_but_weak_text_rejected(self) -> None:
        q = self.ask_a(options=("allow", "deny"), dangerous=True)
        # explicit code, but "1" is not an explicit approval word for a dangerous op
        outcome = self.router.handle_inbound(self.inbound(f"#{q.code} 1"))
        self.assertEqual(outcome, "dangerous_needs_explicit")
        self.assertEqual(self.store.get_question(q.id).state, "pending")


class TestCommands(RouterHarness):
    def test_cancel(self) -> None:
        q = self.ask_a()
        self.assertEqual(self.router.handle_inbound(self.inbound(f"/cancel {q.code}")), "cancelled")
        self.assertEqual(self.store.get_question(q.id).state, "cancelled")

    def test_mute_blocks_answers_until_unmute(self) -> None:
        q = self.ask_a()
        self.router.handle_inbound(self.inbound("/mute"))
        self.assertEqual(self.router.handle_inbound(self.inbound("1")), "muted")
        self.assertEqual(self.store.get_question(q.id).state, "pending")
        self.router.handle_inbound(self.inbound("/unmute"))
        self.assertEqual(self.router.handle_inbound(self.inbound("1")), f"answered:{q.code}")

    def test_pending_and_status(self) -> None:
        self.ask_a()
        self.ask_b()
        self.router.handle_inbound(self.inbound("/pending"))
        self.assertTrue(any("待处理问题 2 个" in s for s in self.sent))
        before = len(self.sent)
        self.router.handle_inbound(self.inbound("/status"))
        self.assertGreater(len(self.sent), before)


if __name__ == "__main__":
    unittest.main()

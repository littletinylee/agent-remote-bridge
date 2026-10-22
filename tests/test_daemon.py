"""Daemon integration tests: concurrency, no crosstalk, delivery resilience,
recovery after restart, and the authenticated loopback API.
"""

from __future__ import annotations

import threading
import time
import unittest

from helpers import ArbTestCase

from arb.channel.console import MemoryChannel
from arb.client import ApiError, Client
from arb.daemon import Daemon
from arb.store import Store


class DaemonHarness(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.owner_ids = ["owner"]
        self.cfg.pairing_code = "PAIR1234"
        self.cfg.notify_min_interval_s = 0
        self.cfg.save()
        self.store = Store(self.cfg.db_path)
        self.channel = MemoryChannel()
        self.daemon = Daemon(self.cfg, channel=self.channel, store=self.store)
        self.daemon.start()

    def tearDown(self) -> None:
        self.daemon.shutdown()
        self.store.close()
        super().tearDown()

    def session(self, agent: str, project: str, key: str) -> dict:
        return self.daemon.resolve_session({"agent": agent, "project": project, "cwd": f"D:/{project}", "key": key})


class TestConcurrentDecisions(DaemonHarness):
    def test_two_agents_two_questions_no_crosstalk(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        b = self.session("codex", "projB", "sb")

        qa = self.daemon.ask(session_id=a["id"], prompt="A: 用 Postgres?", options=["是", "否"])
        qb = self.daemon.ask(session_id=b["id"], prompt="B: 删除分支?", options=["是", "否"])
        self.assertTrue(self.channel.wait_sent(2, timeout=5))

        # both questions must be visible to the human, tagged with their project
        text = "\n".join(m["text"] for m in self.channel.sent)
        self.assertIn("projA", text)
        self.assertIn("projB", text)
        self.assertIn(qa.code, text)
        self.assertIn(qb.code, text)

        # answering B must not touch A
        self.channel.push(f"#{qb.code} 2")
        qb_after = self.daemon.wait_for(qb.id, timeout=5)
        self.assertEqual(qb_after.state, "answered")
        self.assertEqual(qb_after.answer, "否")
        self.assertEqual(self.store.get_question(qa.id).state, "pending")

        self.channel.push(f"#{qa.code} 1")
        qa_after = self.daemon.wait_for(qa.id, timeout=5)
        self.assertEqual(qa_after.state, "answered")
        self.assertEqual(qa_after.answer, "是")

    def test_ask_of_one_agent_does_not_block_other_sessions(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        b = self.session("codex", "projB", "sb")
        qa = self.daemon.ask(session_id=a["id"], prompt="waiting", options=["ok", "no"])
        # while A waits, B can create and resolve its own questions
        qb = self.daemon.ask(session_id=b["id"], prompt="independent", options=["ok", "no"])
        self.assertEqual(self.daemon.wait_for(qa.id, timeout=0.2).state, "pending")
        self.channel.push(f"#{qb.code} 1")
        self.assertEqual(self.daemon.wait_for(qb.id, timeout=5).state, "answered")
        self.assertEqual(self.store.get_question(qa.id).state, "pending")


class TestDeliveryResilience(DaemonHarness):
    def test_outbox_holds_then_flushes_when_channel_recovers(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        self.channel.fail_sends = True
        q = self.daemon.ask(session_id=a["id"], prompt="在通道故障时提问", options=["ok", "no"])
        time.sleep(0.6)
        self.assertEqual(self.channel.sent, [], "must not pretend to deliver while the channel is down")
        # question still answerable locally -> expiry, not silent approval
        self.assertEqual(self.store.get_question(q.id).state, "pending")

        self.channel.fail_sends = False
        self.assertTrue(self.channel.wait_sent(1, timeout=6), "queued message must be delivered after recovery")
        self.assertIn(q.code, self.channel.last_text())

    def test_failed_delivery_never_counts_as_an_answer(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        self.channel.fail_sends = True
        q = self.daemon.ask(session_id=a["id"], prompt="危险操作", options=["allow", "deny"], dangerous=True)
        time.sleep(0.4)
        self.assertEqual(self.store.get_question(q.id).state, "pending")
        self.assertIsNone(self.store.get_question(q.id).answer)


class TestExpiryAndRecovery(DaemonHarness):
    def test_expired_question_notifies_and_defaults_to_deny(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        q = self.daemon.ask(session_id=a["id"], prompt="超时问题", options=["allow", "deny"], dangerous=True)
        self.store.x("UPDATE questions SET expires_at=? WHERE id=?", (time.time() - 1, q.id))
        self.assertTrue(
            self.wait_until(lambda: self.store.get_question(q.id).state == "expired", timeout=6),
            "sweeper must expire the question",
        )
        self.assertIsNone(self.store.get_question(q.id).answer, "expired must not fabricate an approval")

    def test_recover_announces_pending_questions(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        q = self.daemon.ask(session_id=a["id"], prompt="重启后仍需回答", options=["ok", "no"])
        before = len(self.channel.sent)
        info = self.daemon.recover()
        self.assertGreaterEqual(info["pending"], 1)
        self.assertTrue(self.channel.wait_sent(before + 1, timeout=5))
        self.assertTrue(any("重启" in m["text"] and q.code in m["text"] for m in self.channel.sent))

    def test_restart_keeps_pending_questions_answerable(self) -> None:
        a = self.session("claude-code", "projA", "sa")
        q = self.daemon.ask(session_id=a["id"], prompt="跨重启的问题", options=["ok", "no"])
        self.assertTrue(self.channel.wait_sent(1, timeout=5))
        # simulate a crash: kill threads and rebuild a daemon on the same DB
        self.daemon.shutdown()
        store2 = Store(self.cfg.db_path)
        channel2 = MemoryChannel()
        d2 = Daemon(self.cfg, channel=channel2, store=store2)
        d2.start()
        try:
            channel2.push(f"#{q.code} 2")
            q2 = d2.wait_for(q.id, timeout=5)
            self.assertEqual(q2.state, "answered")
            self.assertEqual(q2.answer, "no")
        finally:
            d2.shutdown()
            store2.close()
        # stop the outer harness from shutting down twice
        self.daemon = d2


class TestHttpApi(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.owner_ids = ["owner"]
        self.cfg.save()
        self.store = Store(self.cfg.db_path)
        self.channel = MemoryChannel()
        self.daemon = Daemon(self.cfg, channel=self.channel, store=self.store)
        self.httpd = self.daemon.start_http()
        self.cfg.port = self.httpd.server_address[1]  # bind to :0, then learn the port
        self.cfg.save()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.daemon.start()
        self.client = Client(self.cfg)

    def tearDown(self) -> None:
        self.daemon.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        super().tearDown()

    def test_health_and_auth(self) -> None:
        info = self.client.health()
        self.assertIsNotNone(info)
        self.assertTrue(info["ok"])
        bad = Client(self.cfg)
        bad.cfg = type(self.cfg)(**{**self.cfg.to_dict(), "api_token": "wrong"})
        with self.assertRaises(ApiError) as ctx:
            bad.status()
        self.assertEqual(ctx.exception.status, 401)

    def test_ask_wait_answer_over_http(self) -> None:
        sess = self.client.session(agent="claude-code", project="projA", cwd="D:/projA", key="k1")
        resp = self.client.ask(session_id=sess["id"], prompt="HTTP 提问", options=["yes", "no"], timeout_s=30)
        q = resp["question"]
        self.assertTrue(self.channel.wait_sent(1, timeout=5))
        self.channel.push(f"#{q['code']} 1")
        done = self.client.wait(q["id"], timeout=8)["question"]
        self.assertEqual(done["state"], "answered")
        self.assertEqual(done["answer"], "yes")

    def test_local_answer_endpoint(self) -> None:
        sess = self.client.session(agent="codex", project="projB", cwd="D:/projB", key="k2")
        q = self.client.ask(session_id=sess["id"], prompt="本地回答", options=["yes", "no"])["question"]
        self.client.answer(q["code"], "2")
        done = self.client.wait(q["id"], timeout=5)["question"]
        self.assertEqual(done["state"], "answered")
        self.assertEqual(done["answer"], "no")

    def test_duplicate_inbound_message_id_is_ignored(self) -> None:
        sess = self.client.session(agent="codex", project="projB", cwd="D:/projB", key="k3")
        q = self.client.ask(session_id=sess["id"], prompt="重复投递", options=["yes", "no"])["question"]
        from arb.channel.base import InboundMessage

        msg = InboundMessage(msg_id="dup-1", from_user="owner", text=f"#{q['code']} 1")
        self.daemon.handle_message(msg)
        self.daemon.handle_message(InboundMessage(msg_id="dup-1", from_user="owner", text=f"#{q['code']} 1"))
        self.daemon.handle_message(InboundMessage(msg_id="dup-1", from_user="owner", text=f"#{q['code']} 2"))
        answered = self.store.get_question(q["id"])
        self.assertEqual(answered.state, "answered")
        self.assertEqual(answered.answer, "yes")


if __name__ == "__main__":
    unittest.main()

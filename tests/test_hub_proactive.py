"""M4 proactive suggestions: quiet by default, change-driven, rate limited."""

from __future__ import annotations

import time
import unittest

from helpers import ArbTestCase

from arb.hub import HubStore
from arb.hub_proactive import MAX_PER_HOUR, Proposer, evaluate


def state(nodes=None, pending=None, now=None):
    return {"nodes": nodes or [], "pending": pending or [], "now": now or time.time()}


class TestGate(unittest.TestCase):
    def test_silent_when_nothing_changed(self) -> None:
        st = state([{"name": "a", "online": True, "snapshot": ""}])
        text, sig = evaluate(st, "", 0)
        self.assertIsNone(text)
        text2, _ = evaluate(st, sig, 0)
        self.assertIsNone(text2, "same state twice must not speak twice")

    def test_offline_machine_is_reported_once(self) -> None:
        st = state([{"name": "a", "online": False, "snapshot": ""}])
        text, sig = evaluate(st, "", 0)
        self.assertIn("离线", text or "")
        self.assertIsNone(evaluate(st, sig, 0)[0])

    def test_long_waiting_decision_has_priority(self) -> None:
        st = state(
            [{"name": "a", "online": False, "snapshot": ""}],
            [{"code": "AB34", "node_id": "n1", "created_at": time.time() - 1000}],
        )
        text, _ = evaluate(st, "", 0)
        self.assertIn("#AB34", text or "")
        self.assertIn("建议", text or "")

    def test_rate_limit(self) -> None:
        st = state([{"name": "a", "online": False, "snapshot": ""}])
        text, _ = evaluate(st, "", MAX_PER_HOUR)
        self.assertIsNone(text, "over budget must stay silent")

    def test_dirty_snapshot_suggests_commit(self) -> None:
        st = state([{"name": "a", "online": True, "snapshot": "main 分支, 3 个未提交改动"}])
        text, _ = evaluate(st, "", 0)
        self.assertIn("未提交", text or "")

    def test_clean_snapshot_does_not_nag(self) -> None:
        st = state([{"name": "a", "online": True, "snapshot": "main 分支, 0 个未提交改动"}])
        self.assertIsNone(evaluate(st, "", 0)[0])


class TestProposer(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.home / "hub.sqlite3")
        self.sent: list[str] = []

        class FakeHub:
            def say(inner, text):  # noqa: N805
                self.sent.append(text)
                return "m1"

        self.prop = Proposer(FakeHub(), self.store, interval=0.05)

    def test_tick_sends_at_most_once_per_change(self) -> None:
        self.store.register("a", "t1")
        self.store.x("UPDATE nodes SET last_seen=0")
        first = self.prop.tick()
        second = self.prop.tick()
        self.assertTrue(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.sent), 1)

    def test_audit_records_what_was_said(self) -> None:
        self.store.register("a", "t1")
        self.store.x("UPDATE nodes SET last_seen=0")
        self.prop.tick()
        actions = [r["action"] for r in self.store.q("SELECT action FROM audit")]
        self.assertIn("sent", actions)


if __name__ == "__main__":
    unittest.main()

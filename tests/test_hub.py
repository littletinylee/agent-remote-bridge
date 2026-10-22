"""Hub routing invariants - the security-critical half of the relay.

These run the real HTTP handler against a real SQLite store, with a stub WeChat
client, so what is asserted is the actual wire behaviour:

  * a decision reaches only the machine that asked;
  * an unknown code is refused, never guessed;
  * two pending questions make a bare reply ambiguous -> refused;
  * machine A's inbox never contains machine B's traffic;
  * an offline machine gets its inbox when it comes back;
  * the hub never fabricates an answer.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import ArbTestCase

from arb.hub import Hub, HubServer, HubStore, make_handler


class FakeWeChat:
    """Records what the hub would send to the phone."""

    token = "fake-token"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, to_user: str, text: str, context_token: str = "") -> dict:
        self.sent.append(text)
        return {"message_id": f"m{len(self.sent)}"}

    def get_updates(self, buf: str, timeout: float = 35.0) -> dict:  # pragma: no cover
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}


class HubHarness(ArbTestCase):
    owner = "wxid_owner"

    def setUp(self) -> None:
        super().setUp()
        self.store = HubStore(self.home / "hub.sqlite3")
        self.wx = FakeWeChat()
        self.hub = Hub(self.store, self.wx, [self.owner])  # type: ignore[arg-type]
        self.httpd = HubServer(("127.0.0.1", 0), make_handler(self.hub))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    # -- helpers ---------------------------------------------------------
    def call(self, path: str, payload: dict | None = None, timeout: float = 10) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def register(self, name: str, token: str = "tok") -> str:
        return str(self.call("/v1/register", {"name": name, "token": token})["node_id"])

    def inbox(self, node_id: str, token: str = "tok", timeout: float = 0.2) -> list[dict]:
        return self.call("/v1/poll", {"node_id": node_id, "token": token, "timeout": timeout})["messages"]

    def human(self, text: str, msg_id: str | None = None) -> str:
        return self.hub.handle_human(msg_id or f"m-{text}-{len(self.wx.sent)}", self.owner, text)


class TestRegistration(HubHarness):
    def test_register_and_reject_wrong_token(self) -> None:
        node = self.register("laptop")
        self.assertTrue(node)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.inbox(node, token="wrong")
        self.assertEqual(ctx.exception.code, 401)

    def test_same_name_needs_the_same_token(self) -> None:
        self.register("laptop", "tok")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.register("laptop", "other")
        self.assertEqual(ctx.exception.code, 403)


class TestDecisionRouting(HubHarness):
    def test_question_reaches_only_the_asking_machine(self) -> None:
        a = self.register("machine-a")
        b = self.register("machine-b")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "要不要删库"})
        self.assertIn("#AB34", "\n".join(self.wx.sent))

        self.human("#AB34 yes")
        self.assertTrue(self.inbox(a), "the asking machine must receive the answer")
        self.assertEqual(self.inbox(b), [], "the other machine must receive nothing")

    def test_unknown_code_is_refused(self) -> None:
        self.register("machine-a")
        outcome = self.human("#ZZZZ yes")
        self.assertEqual(outcome, "unknown_code")
        self.assertTrue(any("没有 #ZZZZ" in s for s in self.wx.sent))

    def test_two_pending_questions_refuse_bare_text(self) -> None:
        a = self.register("machine-a")
        b = self.register("machine-b")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "A 的问题"})
        self.call("/v1/question", {"node_id": b, "token": "tok", "code": "CD56", "prompt": "B 的问题"})
        self.assertEqual(self.human("随便回一句"), "ambiguous")
        self.assertEqual(self.inbox(a), [])
        self.assertEqual(self.inbox(b), [])
        self.assertTrue(any("AB34" in s and "CD56" in s for s in self.wx.sent), "must list both codes")

    def test_single_pending_accepts_bare_text(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "只有一个问题"})
        self.assertEqual(self.human("同意"), "forwarded_sole")
        msgs = self.inbox(a)
        self.assertEqual(msgs[0]["text"], "同意")
        self.assertEqual(msgs[0]["code"], "AB34")

    def test_node_owns_question_state(self) -> None:
        """The hub forwards and leaves the code open; only the node closes it.

        Before this changed, the hub said "已收到" even when the machine then
        rejected the answer (e.g. "使用" is not allow/deny), which is a false
        success for a dangerous operation.
        """
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.assertEqual(self.human("#AB34 yes"), "forwarded")
        self.assertEqual(self.store.resolve("AB34")["state"], "pending", "hub must not close it")
        self.call("/v1/question/close", {"node_id": a, "token": "tok", "code": "AB34"})
        self.assertEqual(self.store.resolve("AB34")["state"], "closed")

    def test_hub_never_invents_an_answer(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.assertEqual(self.inbox(a), [], "asking a question must not deliver anything to the node")
        self.assertTrue(self.store.resolve("AB34") is not None)


    def test_quoted_reply_binds_to_the_exact_message(self) -> None:
        """Two questions at once: quoting one of them must win over #CODE rules."""
        a = self.register("machine-a")
        b = self.register("machine-b")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "A 的问题"})
        self.call("/v1/question", {"node_id": b, "token": "tok", "code": "CD56", "prompt": "B 的问题"})
        q = self.store.resolve("CD56")
        self.assertTrue(q["msg_id"], "the pushed message id must be stored")
        self.assertEqual(self.hub.handle_human("q1", self.owner, "同意", ref_svr_id=q["msg_id"]), "forwarded_quote")
        self.assertEqual(self.inbox(a), [], "must not leak to the other machine")
        msgs = self.inbox(b)
        self.assertEqual(msgs[0]["code"], "CD56")
        self.assertEqual(msgs[0]["text"], "同意")

    def test_quote_of_a_handled_question_is_ignored(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        mid = self.store.resolve("AB34")["msg_id"]
        self.hub.handle_human("q1", self.owner, "yes", ref_svr_id=mid)
        self.call("/v1/question/close", {"node_id": a, "token": "tok", "code": "AB34"})
        self.assertEqual(self.hub.handle_human("q2", self.owner, "yes", ref_svr_id=mid), "already_handled")
        self.assertEqual(len(self.inbox(a)), 1)


class TestQuestionTtl(HubHarness):
    def test_stale_pending_is_expired_not_routed(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "old"})
        self.store.x("UPDATE questions SET created_at = created_at - 7200 WHERE code='AB34'")
        self.assertEqual(self.store.reap(1800), 1)
        self.assertEqual(self.store.resolve("AB34")["state"], "expired")
        self.assertEqual(self.human("#AB34 yes"), "unknown_code", "an expired code must not be routable")
        self.assertEqual(self.inbox(a), [])

    def test_recent_question_survives(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "new"})
        self.assertEqual(self.store.reap(1800), 0)
        self.assertEqual(self.store.resolve("AB34")["state"], "pending")


class TestQuoteToMachine(HubHarness):
    """A quoted notification must be deliverable, not just rejected."""

    def test_quote_of_a_notification_goes_back_to_that_machine(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/send", {"node_id": a, "token": "tok", "text": "M3 完成，请试玩确认", "kind": "notify"})
        self.assertEqual(self.hub.handle_human("q1", self.owner, "ok", "", "m1"), "forwarded_to_machine")
        msgs = self.inbox(a)
        self.assertEqual(msgs[0]["kind"], "human")
        self.assertEqual(msgs[0]["text"], "ok")
        self.assertEqual(msgs[0]["frm"], self.owner)

    def test_quote_of_an_unknown_message_stays_friendly(self) -> None:
        self.register("machine-a")
        self.assertEqual(self.hub.handle_human("q2", self.owner, "ok", "", "999999"), "unknown_quote")

    def test_question_quote_still_wins_over_notification_routing(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        mid = self.store.resolve("AB34")["msg_id"]
        self.assertEqual(self.hub.handle_human("q3", self.owner, "yes", "", mid), "forwarded_quote")
        self.assertEqual(self.inbox(a)[0]["kind"], "answer")


class TestReplyByIndex(HubHarness):
    """The owner cannot quote a bot message on WeChat, so a bare number must work."""

    def _two(self):
        a = self.register("machine-a")
        b = self.register("machine-b")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "A 的问题"})
        self.call("/v1/question", {"node_id": b, "token": "tok", "code": "CD56", "prompt": "B 的问题"})
        return a, b

    def test_bare_number_answers_the_item_it_labelled(self) -> None:
        a, b = self._two()
        self.assertEqual(self.human("先看看"), "ambiguous")   # prints "1) ... 2) ..."
        saved = json.loads(self.store.kv_get("pending_list", "{}"))
        self.assertEqual(len(saved["codes"]), 2, "the printed list must be remembered")
        want = saved["codes"][1]

        self.assertEqual(self.human("2"), "forwarded_index")
        hits = [m for m in (self.inbox(a) + self.inbox(b)) if m["kind"] == "answer"]
        self.assertEqual(len(hits), 1, "exactly one machine receives it")
        self.assertEqual(hits[0]["code"], want, "and it is the one labelled 2)")
        self.assertEqual(hits[0]["text"], "2")

    def test_out_of_range_index_is_refused(self) -> None:
        a, b = self._two()
        self.assertEqual(self.human("先看看"), "ambiguous")
        self.assertEqual(self.human("9"), "bad_index")
        self.assertEqual(self.inbox(a), [])
        self.assertEqual(self.inbox(b), [])

    def test_number_without_a_recent_list_is_not_guessed(self) -> None:
        a, b = self._two()
        self.assertEqual(self.human("1"), "bad_index")
        self.assertEqual(self.inbox(a), [])
        self.assertEqual(self.inbox(b), [])


class TestSenderForwarding(HubHarness):
    def test_inbox_carries_the_real_wechat_sender(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.assertEqual(self.human("#AB34 yes"), "forwarded")
        msg = self.inbox(a)[0]
        self.assertEqual(msg["frm"], self.owner, "the node re-checks this against its own allowlist")

    def test_direct_message_also_carries_the_sender(self) -> None:
        a = self.register("machine-a")
        self.human("/m machine-a 跑一下测试")
        self.assertEqual(self.inbox(a)[0]["frm"], self.owner)

    def test_unknown_sender_is_never_forwarded(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.assertEqual(self.hub.handle_human("x", "stranger", "#AB34 yes"), "unauthorized")
        self.assertEqual(self.inbox(a), [])


class TestIsolationAndBuffering(HubHarness):
    def test_cross_machine_traffic_is_isolated(self) -> None:
        a = self.register("machine-a")
        b = self.register("machine-b")
        self.call("/v1/send", {"node_id": a, "token": "tok", "text": "A 的进度", "kind": "progress"})
        self.assertEqual(self.inbox(b), [])
        self.assertTrue(any("[machine-a]" in s and "A 的进度" in s for s in self.wx.sent))

    def test_offline_machine_gets_buffered_messages_on_return(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.human("#AB34 先去吃饭")            # machine is 'offline' (nobody polls)
        msgs = self.inbox(a, timeout=0.2)       # ... comes back later
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["text"], "先去吃饭")
        self.assertEqual(self.inbox(a, timeout=0.2), [], "delivered once, not twice")

    def test_duplicate_wechat_delivery_is_ignored(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.assertEqual(self.human("#AB34 ok", msg_id="dup-1"), "forwarded")
        self.assertEqual(self.human("#AB34 ok", msg_id="dup-1"), "duplicate")
        self.assertEqual(len(self.inbox(a)), 1)

    def test_unauthorized_sender_is_ignored(self) -> None:
        a = self.register("machine-a")
        self.call("/v1/question", {"node_id": a, "token": "tok", "code": "AB34", "prompt": "q"})
        self.assertEqual(self.hub.handle_human("x", "stranger", "#AB34 yes"), "unauthorized")
        self.assertEqual(self.inbox(a), [])


class TestHubCommands(HubHarness):
    def test_machines_and_status(self) -> None:
        self.register("laptop")
        self.human("/machines")
        self.assertTrue(any("laptop" in s for s in self.wx.sent))
        self.human("/status")
        self.assertTrue(any("Hub" in s for s in self.wx.sent))

    def test_direct_message_to_one_machine(self) -> None:
        a = self.register("machine-a")
        self.human("/m machine-a 把测试跑一遍")
        self.assertEqual(self.inbox(a)[0]["text"], "把测试跑一遍")


if __name__ == "__main__":
    unittest.main()

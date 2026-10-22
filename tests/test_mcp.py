"""MCP protocol tests: framing, handshake, tool surface and the safety rules of
ask_human (timeouts authorise nothing)."""

from __future__ import annotations

import io
import json
import unittest

from helpers import ArbTestCase

from arb import mcp
from arb.client import ApiError, DaemonUnavailable


class FakeClient:
    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.state = "answered"
        self.answer = "allow"
        self.notifications: list[dict] = []

    def ensure(self, *a, **k):
        return {"ok": True}

    def session(self, **kw):
        return {"id": "s1", "project": kw.get("project"), "agent": kw.get("agent")}

    def ask(self, **kw):
        self.asked.append(kw)
        return {"question": {"id": "q1", "code": "AB12", "state": "pending"}}

    def question(self, **kw):
        return {"question": {"id": "q1", "code": "AB12", "state": self.state, "answer": self.answer}}

    def wait(self, qid, timeout):
        return {"question": {"id": qid, "code": "AB12", "state": self.state, "answer": self.answer}}

    def notify(self, **kw):
        self.notifications.append(kw)
        return {"ok": True}

    def pending(self):
        return {"pending": [{"code": "AB12", "prompt": "p", "dangerous": False}]}


class TestFraming(unittest.TestCase):
    def test_newline_framing(self) -> None:
        stream = io.BytesIO(b'{"jsonrpc":"2.0","id":1}\n')
        self.assertEqual(mcp.read_frame(stream), b'{"jsonrpc":"2.0","id":1}')

    def test_content_length_framing(self) -> None:
        body = b'{"jsonrpc":"2.0","id":2}'
        stream = io.BytesIO(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        self.assertEqual(mcp.read_frame(stream), body)

    def test_eof(self) -> None:
        self.assertIsNone(mcp.read_frame(io.BytesIO(b"")))

    def test_written_frame_is_one_line_json(self) -> None:
        out = io.BytesIO()
        mcp.write_frame(out, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        line = out.getvalue()
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(json.loads(line), {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})


class TestProtocol(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = mcp.McpServer(agent="test-agent")
        self.server.client = FakeClient()

    def test_initialize(self) -> None:
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_tools_list_exposes_ask_human(self) -> None:
        resp = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"ask_human", "notify", "progress", "check_answer", "list_pending"})
        ask = next(t for t in resp["result"]["tools"] if t["name"] == "ask_human")
        self.assertIn("prompt", ask["inputSchema"]["required"])

    def test_initialized_notification_has_no_response(self) -> None:
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_an_error(self) -> None:
        resp = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool_is_an_error(self) -> None:
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
        )
        self.assertEqual(resp["error"]["code"], -32601)


class TestAskHumanTool(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = mcp.McpServer(agent="test-agent")
        self.fake = FakeClient()
        self.server.client = self.fake

    def call(self, name: str, args: dict) -> dict:
        resp = self.server.handle(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": args}}
        )
        return json.loads(resp["result"]["content"][0]["text"])

    def test_answered_returns_the_answer_and_not_an_approval_summary(self) -> None:
        self.fake.state = "answered"
        self.fake.answer = "deny"
        out = self.call("ask_human", {"prompt": "允许删除吗", "options": ["allow", "deny"], "dangerous": True})
        self.assertEqual(out["status"], "answered")
        self.assertEqual(out["answer"], "deny")
        self.assertEqual(out["code"], "AB12")
        self.assertTrue(self.fake.asked[0]["dangerous"])

    def test_timeout_explicitly_says_not_approved(self) -> None:
        self.fake.state = "expired"
        out = self.call("ask_human", {"prompt": "危险操作?", "dangerous": True})
        self.assertEqual(out["status"], "timeout")
        self.assertFalse(out["approval"])
        self.assertIn("refusal", out["guidance"])

    def test_daemon_down_is_an_error_not_an_approval(self) -> None:
        def boom(**kw):
            raise DaemonUnavailable("no daemon")

        self.fake.ask = boom  # type: ignore[method-assign]
        resp = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "ask_human", "arguments": {"prompt": "x"}},
            }
        )
        self.assertTrue(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload.get("approval", False))

    def test_empty_prompt_is_rejected(self) -> None:
        resp = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "ask_human", "arguments": {"prompt": "  "}},
            }
        )
        self.assertTrue(resp["result"]["isError"])

    def test_notify_and_progress(self) -> None:
        self.call("notify", {"title": "开始", "body": "b", "kind": "start"})
        self.call("progress", {"message": "50%"})
        self.assertEqual(len(self.fake.notifications), 2)
        self.assertEqual(self.fake.notifications[1]["kind"], "progress")

    def test_check_answer_and_list_pending(self) -> None:
        self.fake.state = "pending"
        self.assertEqual(self.call("check_answer", {"code": "#AB12"})["status"], "pending")
        self.assertEqual(len(self.call("list_pending", {})["pending"]), 1)


if __name__ == "__main__":
    unittest.main()

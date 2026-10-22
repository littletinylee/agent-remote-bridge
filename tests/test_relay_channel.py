"""RelayChannel against a fake hub - the layer that broke in production.

Production failure: every answer relayed through the hub was rejected locally
with '未授权微信号的消息，来源ID: hub' because the channel presented the sender
as a literal. These tests drive the real RelayChannel against a real HTTP
server, so the contract (real sender id, '#CODE text' re-feed, per-code
registration and close) is pinned without a phone.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ArbTestCase

from arb.channel.relay import RelayChannel

OWNER = "o9cq8050GDr6ElW4DLz66D7_-a9c@im.wechat"


class FakeHub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        return

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        state = self.server.state  # type: ignore[attr-defined]
        state.setdefault("calls", []).append((self.path, body))
        if self.path == "/v1/register":
            self._reply({"ok": True, "node_id": "VNyXQQZs8nE", "name": body.get("name")})
        elif self.path == "/v1/poll":
            msgs = state.pop("next", [])
            self._reply({"ok": True, "messages": msgs})
        else:
            self._reply({"ok": True})


class TestRelayChannel(ArbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
        self.httpd.state = {}  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cfg.channel = "relay"
        self.cfg.hub_url = f"http://127.0.0.1:{self.port}"
        self.cfg.hub_node_name = "workbench"
        self.cfg.hub_token = "tok-123"
        self.cfg.hub_node_id = ""
        self.cfg.save()
        self.channel = RelayChannel(self.cfg, None)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def calls(self, path: str) -> list[dict]:
        return [b for p, b in self.httpd.state.get("calls", []) if p == path]  # type: ignore[attr-defined]

    def test_answer_keeps_the_real_sender_and_the_code(self) -> None:
        """The regression: the node must see the owner's wxid, not 'hub'."""
        self.httpd.state["next"] = [  # type: ignore[attr-defined]
            {"kind": "answer", "text": "yes", "code": "RCHR", "frm": OWNER}
        ]
        got: list = []
        stop = threading.Event()
        thread = threading.Thread(target=self.channel.start, args=(got.append, stop), daemon=True)
        thread.start()
        for _ in range(60):
            if got:
                break
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=5)

        self.assertTrue(got, "the channel must deliver the polled message")
        msg = got[0]
        self.assertEqual(msg.from_user, OWNER, "sender must be the real wxid")
        self.assertEqual(msg.text, "#RCHR yes", "answer is re-fed as a #CODE reply for local re-checks")
        self.assertEqual(self.cfg.hub_node_id, "VNyXQQZs8nE", "registration id is persisted")

    def test_plain_human_message_is_not_rewritten(self) -> None:
        self.httpd.state["next"] = [  # type: ignore[attr-defined]
            {"kind": "human", "text": "把测试跑一遍", "code": "", "frm": OWNER}
        ]
        got: list = []
        stop = threading.Event()
        thread = threading.Thread(target=self.channel.start, args=(got.append, stop), daemon=True)
        thread.start()
        for _ in range(60):
            if got:
                break
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=5)
        self.assertEqual(got[0].text, "把测试跑一遍")
        self.assertEqual(got[0].from_user, OWNER)

    def test_question_is_registered_with_its_code(self) -> None:
        self.httpd.state["next"] = []  # type: ignore[attr-defined]
        self.channel.register_question("AB34", "要不要删库")
        body = self.calls("/v1/question")[0]
        self.assertEqual(body["code"], "AB34")
        self.assertEqual(body["node_id"], "")  # before start() registers
        self.assertEqual(body["token"], "tok-123")

    def test_send_and_close_use_this_nodes_credentials(self) -> None:
        self.cfg.hub_node_id = "node-1"
        self.channel = RelayChannel(self.cfg, None)
        self.channel.send("一轮任务结束")
        self.channel.close_question("AB34")
        self.assertEqual(self.calls("/v1/send")[0]["node_id"], "node-1")
        self.assertEqual(self.calls("/v1/question/close")[0]["code"], "AB34")


if __name__ == "__main__":
    unittest.main()

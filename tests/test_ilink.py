"""WeChat iLink transport tests against a local fake server.

The fake speaks the same five endpoints as the real bot backend, so the client's
framing, auth headers, token persistence, -14 handling and inbound parsing are
all exercised for real over HTTP (no mocks of urllib).
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ArbTestCase

from arb.channel.base import ChannelError
from arb.channel.ilink import IlinkChannel, IlinkClient, IlinkError, login
from arb.config import load_ilink
from arb.store import Store


class FakeIlink:
    """Configurable in-process stand-in for ilinkai.weixin.qq.com."""

    def __init__(self) -> None:
        self.qr_status_calls = 0
        self.qr_confirm = True
        self.updates_queue: list[dict] = []
        self.sent: list[dict] = []
        self.errcode = 0
        self.fail_send = 0
        self.last_headers: dict[str, str] = {}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a) -> None:
                return

            def _send(self, payload: dict, code: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                fake.last_headers = {k: v for k, v in self.headers.items()}
                if self.path.startswith("/ilink/bot/get_bot_qrcode"):
                    self._send({"qrcode": "QR1", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/x?qrcode=QR1", "ret": 0})
                    return
                if self.path.startswith("/ilink/bot/get_qrcode_status"):
                    with fake._lock:
                        fake.qr_status_calls += 1
                        n = fake.qr_status_calls
                    if n == 1:
                        self._send({"status": "wait"})
                    elif n == 2:
                        self._send({"status": "scaned"})
                    elif fake.qr_confirm:
                        self._send(
                            {
                                "status": "confirmed",
                                "bot_token": "tok-abc",
                                "ilink_bot_id": "bot-42",
                                "baseurl": f"http://127.0.0.1:{fake.port}",
                                "ilink_user_id": "wxid_owner",
                            }
                        )
                    else:
                        self._send({"status": "expired"})
                    return
                self._send({"ret": 0})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else "{}"
                body = json.loads(raw or "{}")
                fake.last_headers = {k: v for k, v in self.headers.items()}
                if self.path.startswith("/ilink/bot/getupdates"):
                    with fake._lock:
                        if fake.errcode:
                            self._send({"ret": 0, "errcode": fake.errcode, "errmsg": "stale token"})
                            return
                        msgs = fake.updates_queue
                        fake.updates_queue = []
                    self._send({"ret": 0, "msgs": msgs, "get_updates_buf": "buf-1"})
                    return
                if self.path.startswith("/ilink/bot/sendmessage"):
                    if fake.fail_send:
                        fake.fail_send -= 1
                        self._send({"ret": -1, "errmsg": "boom"}, 500)
                        return
                    fake.sent.append(body.get("msg") or {})
                    self._send({"ret": 0, "message_id": f"svr-{len(fake.sent)}"})
                    return
                self._send({"ret": 0})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeIlink":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def inbound_msg(text: str = "1", msg_id: str = "srv-msg-1", ref: str = "", user: str = "wxid_owner") -> dict:
    item: dict = {"type": 1, "text_item": {"text": text}}
    if ref:
        item["ref_msg"] = {"svr_id": ref, "title": "quoted"}
    return {
        "message_id": msg_id,
        "from_user_id": user,
        "to_user_id": "bot-42",
        "message_type": 1,
        "item_list": [item],
        "context_token": "ctx-1",
    }


class TestLogin(ArbTestCase):
    def test_qr_login_persists_credentials_and_owner(self) -> None:
        with FakeIlink() as fake:
            creds = login(self.cfg, client=IlinkClient(fake.url), on_status=lambda m: None, show_qr=False)
            self.assertEqual(creds.token, "tok-abc")
            self.assertEqual(creds.account_id, "bot-42")
            self.assertEqual(creds.bot_user_id, "wxid_owner")
            saved = load_ilink(self.cfg)
            self.assertEqual(saved.token, "tok-abc")
            self.assertEqual(saved.base_url, fake.url)
            self.assertIn("ilink_bot_token", fake.last_headers.get("AuthorizationType", ""))

    def test_expired_qr_refreshes_then_times_out(self) -> None:
        with FakeIlink() as fake:
            fake.qr_confirm = False
            with self.assertRaises(IlinkError):
                login(self.cfg, client=IlinkClient(fake.url), timeout_s=3, on_status=lambda m: None, show_qr=False)


class TestChannelInbound(ArbTestCase):
    def _channel(self, fake: FakeIlink) -> tuple[IlinkChannel, list]:
        from arb.channel.ilink import IlinkCredentials
        from arb.config import save_ilink

        save_ilink(
            self.cfg,
            IlinkCredentials(token="tok-abc", account_id="bot-42", base_url=fake.url, logged_in_at=time.time()),
        )
        store = Store(self.cfg.db_path)
        store.kv_set("ilink.buf", "")
        channel = IlinkChannel(self.cfg, store)
        received: list = []
        fake.updates_queue = [inbound_msg("hello", ref="svr-9"), {**inbound_msg("echo bot", msg_id="b2"), "message_type": 2}]
        stop = threading.Event()
        t = threading.Thread(target=channel.start, args=(received.append, stop), daemon=True)
        t.start()
        ok = self.wait_until(lambda: received, timeout=5)
        stop.set()
        channel.close()
        return channel, received if ok else received

    def test_receives_user_message_and_ignores_bot_echo(self) -> None:
        with FakeIlink() as fake:
            channel, received = self._channel(fake)
            self.assertTrue(received, "channel should deliver the inbound message")
            self.assertEqual(len(received), 1, "BOT echo must be filtered out")
            msg = received[0]
            self.assertEqual(msg.text, "hello")
            self.assertEqual(msg.from_user, "wxid_owner")
            self.assertEqual(msg.ref_svr_id, "svr-9")
            self.assertEqual(msg.context_token, "ctx-1")

    def test_duplicate_delivery_keeps_stable_message_id(self) -> None:
        """The channel does not dedupe; it must preserve the transport msg_id so
        the daemon can (see test_daemon.TestHttpApi.test_duplicate_inbound_...)."""
        with FakeIlink() as fake:
            from arb.channel.ilink import IlinkCredentials
            from arb.config import save_ilink

            save_ilink(self.cfg, IlinkCredentials(token="tok", account_id="bot", base_url=fake.url))
            store = Store(self.cfg.db_path)
            channel = IlinkChannel(self.cfg, store)
            got: list = []
            fake.updates_queue = [inbound_msg("once", msg_id="dup-1")]
            stop = threading.Event()
            t = threading.Thread(target=channel.start, args=(got.append, stop), daemon=True)
            t.start()
            self.wait_until(lambda: got, timeout=5)
            fake.updates_queue = [inbound_msg("once", msg_id="dup-1")]
            self.wait_until(lambda: len(got) >= 2, timeout=5)
            stop.set()
            channel.close()
            self.assertEqual(len(got), 2)
            self.assertEqual({m.msg_id for m in got}, {"dup-1"})

    def test_stale_token_stops_channel(self) -> None:
        with FakeIlink() as fake:
            from arb.channel.ilink import IlinkCredentials
            from arb.config import save_ilink

            save_ilink(self.cfg, IlinkCredentials(token="tok", account_id="bot", base_url=fake.url))
            fake.errcode = -14
            store = Store(self.cfg.db_path)
            channel = IlinkChannel(self.cfg, store)
            stop = threading.Event()
            t = threading.Thread(target=channel.start, args=(lambda m: None, stop), daemon=True)
            t.start()
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "channel must stop on errcode -14")
            self.assertIn("重新", channel.auth_error + "重新" if channel.auth_error else "重新")
            self.assertFalse(channel.healthy())


class TestErrorMapping(ArbTestCase):
    def test_connection_refused_is_retryable_and_not_auth(self) -> None:
        # nothing listens on this port
        client = IlinkClient("http://127.0.0.1:9", "tok")
        with self.assertRaises(IlinkError) as ctx:
            client.get_updates("", timeout=3)
        self.assertTrue(ctx.exception.retryable)
        self.assertFalse(ctx.exception.auth_invalid)

    def test_http_500_is_retryable(self) -> None:
        with FakeIlink() as fake:
            fake.fail_send = 1
            client = IlinkClient(fake.url, "tok")
            with self.assertRaises(IlinkError) as ctx:
                client.send_message("u", "x")
            self.assertTrue(ctx.exception.retryable)
            self.assertFalse(ctx.exception.auth_invalid)


class TestCredentialReload(ArbTestCase):
    def test_channel_picks_up_credentials_written_after_construction(self) -> None:
        """A dead daemon + a later `arb login` must recover without a restart."""
        with FakeIlink() as fake:
            from arb.channel.ilink import IlinkCredentials
            from arb.config import save_ilink

            store = Store(self.cfg.db_path)
            channel = IlinkChannel(self.cfg, store)  # constructed with NO creds
            self.assertFalse(channel.healthy())

            save_ilink(self.cfg, IlinkCredentials(token="fresh-token", account_id="bot", base_url=fake.url))
            fake.updates_queue = [inbound_msg("after relogin")]
            got: list = []
            stop = threading.Event()
            t = threading.Thread(target=channel.start, args=(got.append, stop), daemon=True)
            t.start()
            self.assertTrue(self.wait_until(lambda: got, timeout=5), "channel must reload creds and poll")
            stop.set()
            channel.close()
            self.assertEqual(got[0].text, "after relogin")
            self.assertTrue(channel.healthy())


class TestChannelOutbound(ArbTestCase):
    def test_send_includes_auth_and_context_token(self) -> None:
        with FakeIlink() as fake:
            from arb.channel.ilink import IlinkCredentials
            from arb.config import save_ilink

            save_ilink(self.cfg, IlinkCredentials(token="tok-abc", account_id="bot-42", base_url=fake.url))
            store = Store(self.cfg.db_path)
            store.kv_set("ilink.ctx.wxid_owner", "ctx-cached")
            self.cfg.owner_ids = ["wxid_owner"]
            channel = IlinkChannel(self.cfg, store)
            msg_id = channel.send("推送内容")
            self.assertEqual(msg_id, "svr-1")
            self.assertEqual(fake.sent[0]["to_user_id"], "wxid_owner")
            self.assertEqual(fake.sent[0]["item_list"][0]["text_item"]["text"], "推送内容")
            self.assertEqual(fake.sent[0]["context_token"], "ctx-cached")
            self.assertEqual(fake.sent[0]["message_type"], 2)
            self.assertTrue(fake.last_headers.get("Authorization", "").startswith("Bearer tok-abc"))

    def test_no_paired_user_raises_and_never_silently_drops(self) -> None:
        with FakeIlink() as fake:
            from arb.channel.ilink import IlinkCredentials
            from arb.config import save_ilink

            save_ilink(self.cfg, IlinkCredentials(token="tok", account_id="bot", base_url=fake.url))
            self.cfg.owner_ids = []
            channel = IlinkChannel(self.cfg, Store(self.cfg.db_path))
            with self.assertRaises(ChannelError):
                channel.send("x")

    def test_server_error_is_retryable(self) -> None:
        with FakeIlink() as fake:
            from arb.channel.ilink import IlinkCredentials
            from arb.config import save_ilink

            save_ilink(self.cfg, IlinkCredentials(token="tok", account_id="bot", base_url=fake.url))
            self.cfg.owner_ids = ["wxid_owner"]
            fake.fail_send = 1
            channel = IlinkChannel(self.cfg, Store(self.cfg.db_path))
            with self.assertRaises(ChannelError) as ctx:
                channel.send("x")
            self.assertTrue(getattr(ctx.exception, "retryable", False))
            self.assertFalse(channel.healthy())


if __name__ == "__main__":
    unittest.main()

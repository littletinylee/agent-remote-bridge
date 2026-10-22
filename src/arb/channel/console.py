"""Local channels.

* :class:`ConsoleChannel` — the terminal is the remote: useful on day one, in
  CI, and as the documented fallback when WeChat is unreachable.
* :class:`MemoryChannel` — deterministic in-process channel used by tests.
"""

from __future__ import annotations

import queue
import sys
import threading
import time

from ..util import new_id
from .base import Channel, ChannelError, InboundMessage, OnMessage


class ConsoleChannel(Channel):
    name = "console"

    def __init__(self, stream=None):
        self._stream = stream or sys.stdout
        self._healthy = True
        self._closed = False

    def start(self, on_message: OnMessage, stop_event: threading.Event) -> None:
        def reader() -> None:
            for line in sys.stdin:
                if stop_event.is_set() or self._closed:
                    return
                text = line.strip()
                if not text:
                    continue
                on_message(
                    InboundMessage(
                        msg_id=new_id("in"),
                        from_user="console",
                        text=text,
                    )
                )

        threading.Thread(target=reader, name="console-in", daemon=True).start()
        while not stop_event.is_set() and not self._closed:
            stop_event.wait(0.5)

    def send(self, text: str, to: str | None = None) -> str:
        if self._closed:
            raise ChannelError("console channel closed", retryable=False)
        msg_id = new_id("out")
        print(f"\n─── 微信(console) ───\n{text}\n─────────────────────", file=self._stream, flush=True)
        return msg_id

    def healthy(self) -> bool:
        return self._healthy and not self._closed

    def close(self) -> None:
        self._closed = True


class MemoryChannel(Channel):
    """In-memory channel: tests push inbound, assert on outbound."""

    name = "memory"

    def __init__(self, fail_sends: bool = False):
        self.sent: list[dict] = []
        self._inbox: "queue.Queue[InboundMessage]" = queue.Queue()
        self._on_message: OnMessage | None = None
        self._healthy = True
        self.fail_sends = fail_sends
        self.sent_event = threading.Event()

    # test helpers
    def push(self, text: str, from_user: str = "owner", ref_svr_id: str = "", msg_id: str | None = None) -> InboundMessage:
        msg = InboundMessage(msg_id=msg_id or new_id("in"), from_user=from_user, text=text, ref_svr_id=ref_svr_id)
        self._inbox.put(msg)
        return msg

    def wait_sent(self, count: int = 1, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.sent) >= count:
                return True
            time.sleep(0.02)
        return False

    def last_text(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""

    def set_healthy(self, ok: bool) -> None:
        self._healthy = ok

    # Channel API
    def start(self, on_message: OnMessage, stop_event: threading.Event) -> None:
        self._on_message = on_message
        while not stop_event.is_set():
            try:
                msg = self._inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                on_message(msg)
            except Exception as exc:  # keep the loop alive like the real channel
                print(f"[memory] handler error: {exc}", file=sys.stderr)

    def send(self, text: str, to: str | None = None) -> str:
        if self.fail_sends or not self._healthy:
            raise ChannelError("memory channel down")
        msg_id = new_id("out")
        self.sent.append({"id": msg_id, "text": text, "to": to, "at": time.time()})
        self.sent_event.set()
        return msg_id

    def healthy(self) -> bool:
        return self._healthy and not self.fail_sends

    def close(self) -> None:
        self._healthy = False

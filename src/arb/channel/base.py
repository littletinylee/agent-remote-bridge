"""Channel abstraction: anything that can push text to the human and deliver
inbound replies.

A channel is intentionally dumb: it moves text. Routing, security and question
state all live in the daemon, so swapping WeChat for Telegram/console later is
a single small class.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class InboundMessage:
    msg_id: str
    from_user: str
    text: str
    ref_svr_id: str = ""      # quoted message id (server side), if the human quoted a push
    context_token: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


OnMessage = Callable[[InboundMessage], None]


class Channel(Protocol):
    name: str

    def start(self, on_message: OnMessage, stop_event: threading.Event) -> None: ...

    def send(self, text: str) -> str: ...

    def healthy(self) -> bool: ...

    def close(self) -> None: ...


class ChannelError(RuntimeError):
    """Delivery failed. Callers must treat this as 'not delivered' and never
    as an implicit approval."""

    def __init__(self, message: str, *, auth_invalid: bool = False, retryable: bool = True):
        super().__init__(message)
        self.auth_invalid = auth_invalid
        self.retryable = retryable

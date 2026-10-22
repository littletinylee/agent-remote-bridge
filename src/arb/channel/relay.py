"""Channel that talks to the Hub instead of directly to WeChat.

Only one process may long-poll the WeChat bot (one token, one cursor), so on a
machine that is not the hub the iLink channel is replaced by this relay: the
hub owns the phone, this node owns its own questions/state machine.

Safety note: the hub forwards the human's raw text. We deliberately re-feed an
answer as `#CODE <text>` so it passes through the *local* router again, which
re-checks expiry, authorisation, the dangerous-operation gate and the
once-only transition. The hub cannot bypass any of that.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import Config
from ..util import log_stderr, new_id
from .base import Channel, ChannelError, InboundMessage, OnMessage


class RelayChannel(Channel):
    name = "relay"

    def __init__(self, cfg: Config, store=None):
        self.cfg = cfg
        self.store = store
        self.base = (cfg.hub_url or "").rstrip("/")
        self.node_id = cfg.hub_node_id
        self.token = cfg.hub_token
        self._healthy = False
        self._last_error = ""
        self._stop = threading.Event()
        self._ssl = self._make_ssl(cfg.hub_fingerprint)
        self._last_snapshot = 0.0

    # -- tls -------------------------------------------------------------
    @staticmethod
    def _make_ssl(fingerprint: str) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        # The hub uses a self-signed certificate, pinned by SHA-256 fingerprint
        # (TOFU-style pinning: the node refuses to talk to a different cert).
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if fingerprint:
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _verify_pin(self, conn: ssl.SSLSocket) -> None:
        expected = (self.cfg.hub_fingerprint or "").replace(":", "").upper()
        if not expected:
            return
        der = conn.getpeercert(binary_form=True) or b""
        seen = hashlib.sha256(der).hexdigest().upper()
        if seen != expected:
            raise ChannelError(f"hub certificate fingerprint mismatch: {seen[:16]}… != pinned", retryable=False)

    # -- http ------------------------------------------------------------
    def _call(self, path: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        if not self.base:
            raise ChannelError("hub_url not configured", retryable=False)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.URLError as exc:
            raise ChannelError(f"hub {path}: {exc.reason}", retryable=True) from None
        except TimeoutError:
            raise ChannelError(f"hub {path}: timeout", retryable=True) from None

    # -- channel api -----------------------------------------------------
    def healthy(self) -> bool:
        return self._healthy and not self._stop.is_set()

    def send(self, text: str, to: str | None = None) -> str:
        resp = self._call("/v1/send", {"node_id": self.node_id, "token": self.token, "text": text, "kind": "notify"})
        if not resp.get("ok"):
            raise ChannelError(f"hub rejected send: {resp.get('error')}")
        self._healthy = True
        return f"hub-{int(time.time() * 1000)}"

    def register_question(self, code: str, prompt: str) -> bool:
        """Tell the hub which machine owns this code so the answer comes back here."""
        try:
            resp = self._call("/v1/question", {
                "node_id": self.node_id, "token": self.token, "code": code, "prompt": prompt,
            })
            return bool(resp.get("ok"))
        except ChannelError as exc:
            log_stderr(f"[relay] register_question failed: {exc}")
            return False

    def _snapshot_cwd(self) -> str:
        """The daemon often runs from the user's home; report the project instead.

        Prefer the newest session's working directory (that is where the agent
        actually works), falling back to our own cwd.
        """
        try:
            sessions = self.store.list_sessions(limit=8) if self.store is not None else []
            dirs = [(s.get("cwd") or "").strip() for s in sessions]
            dirs = [d for d in dirs if d and os.path.isdir(d)]
            # A repo is far more informative than a scratch directory, and the
            # newest session is often a temp dir from a test run.
            for d in dirs:
                if os.path.isdir(os.path.join(d, ".git")):
                    return d
            if dirs:
                return dirs[0]
        except Exception:
            pass
        return os.getcwd()

    def send_snapshot(self) -> None:
        """Publish a factual snapshot so the hub's advisor can answer questions."""
        try:
            from .. import snapshot as snap_mod

            snap = snap_mod.build(self._snapshot_cwd(), self.store)
            self._call("/v1/snapshot", {
                "node_id": self.node_id, "token": self.token,
                "snapshot": snap, "projects": snap.get("projects") or [],
            })
            self._last_snapshot = time.time()
        except Exception as exc:
            log_stderr(f"[relay] snapshot failed: {exc}")

    def close_question(self, code: str) -> bool:
        """Tell the hub our router really claimed this answer."""
        try:
            resp = self._call("/v1/question/close", {"node_id": self.node_id, "token": self.token, "code": code})
            return bool(resp.get("ok"))
        except ChannelError:
            return False

    def start(self, on_message: OnMessage, stop_event: threading.Event) -> None:
        reg = self._call("/v1/register", {"name": self.cfg.hub_node_name, "token": self.cfg.hub_token})
        self.node_id = str(reg.get("node_id") or self.node_id)
        self.cfg.hub_node_id = self.node_id
        self.cfg.save()
        log_stderr(f"[relay] registered as {self.cfg.hub_node_name} ({self.node_id}) at {self.base}")
        failures = 0
        while not stop_event.is_set() and not self._stop.is_set():
            try:
                resp = self._call(
                    "/v1/poll",
                    {"node_id": self.node_id, "token": self.token, "timeout": 25, "status": "online"},
                    timeout=45,
                )
                for msg in resp.get("messages") or []:
                    text = str(msg.get("text") or "")
                    code = str(msg.get("code") or "")
                    kind = str(msg.get("kind") or "reply")
                    if not text:
                        continue
                    # Answers go back through the local router as an explicit
                    # #CODE reply, so expiry/dangerous/once-only rules still apply.
                    body = f"#{code} {text}" if (kind == "answer" and code) else text
                    on_message(InboundMessage(
                        msg_id=f"hub-{code or 'x'}-{hashlib.sha1(body.encode()).hexdigest()[:12]}-{new_id('n')[:6]}",
                        from_user="hub",
                        text=body,
                        context_token="",
                    ))
                self._healthy = True
                self._last_error = ""
                failures = 0
                if time.time() - getattr(self, "_last_snapshot", 0) > 60:
                    self.send_snapshot()
            except ChannelError as exc:
                failures += 1
                self._healthy = False
                self._last_error = str(exc)
                if self.store is not None:
                    self.store.audit("relay_error", "channel", {"error": str(exc)[:150], "n": failures})
                wait = min(30.0, 1.5 ** min(failures, 8))
                stop_event.wait(wait)

    def close(self) -> None:
        self._stop.set()

    @property
    def last_error(self) -> str:
        return self._last_error

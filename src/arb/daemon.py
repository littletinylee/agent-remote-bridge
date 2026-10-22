"""The bridge daemon: one process on the Windows box that owns the channel,
the outbox, the question state machine and a loopback HTTP API.

Threads
    channel     long-poll / stdin reader -> Router -> SQLite
    outbox      SQLite -> channel.send, with retry/backoff and persistence
    sweeper     expires stale questions, sends timeout notices
    http        loopback JSON API used by MCP servers, hooks and the CLI

Crash safety: questions and outbox rows are rows in SQLite, so a restart
re-delivers anything unsent and keeps every pending decision answerable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import text as T
from .channel.base import Channel, ChannelError, InboundMessage
from .config import Config, load, load_ilink
from .router import Router
from .store import Question, Store
from .util import iso, json_dumps, json_loads, log_stderr, new_id, now, one_line, secret_compare, truncate

VERSION = "0.4.0"

# Trusted local identities: the loopback API is already token-authenticated, so
# `arb answer` / hooks are not subject to the WeChat owner allowlist.
LOCAL_SENDERS = {"local-cli", "cli", "local", "hook"}


class _LoopbackServer(ThreadingHTTPServer):
    """Refuse to share a port: a second daemon must fail loudly, not hijack.

    (Windows honours SO_REUSEADDR by letting a second socket steal the port,
    which would silently split brain the bridge.)
    """

    allow_reuse_address = False
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        # Probes (arb status, health checks) close their socket abruptly; that is
        # normal, not a bug worth a full traceback in the log.
        import sys

        exc = sys.exc_info()[1]
        log_stderr(f"[http] client {client_address[0]} dropped: {type(exc).__name__}: {exc}")


def chunk_text(text: str, limit: int) -> list[str]:
    """Split a message on line boundaries so no single push is oversized."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # pathological single line
            if cur:
                chunks.append("".join(cur))
                cur, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit and cur:
            chunks.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return [c for c in chunks if c.strip()]


def make_channel(cfg: Config, store: Store) -> Channel:
    if cfg.channel == "console":
        from .channel.console import ConsoleChannel

        return ConsoleChannel()
    from .channel.ilink import IlinkChannel

    return IlinkChannel(cfg, store)


class Daemon:
    def __init__(self, cfg: Config | None = None, channel: Channel | None = None, store: Store | None = None):
        self.cfg = cfg or load()
        self.store = store or Store(self.cfg.db_path)
        self.channel = channel or make_channel(self.cfg, self.store)
        self.router = Router(self.cfg, self.store, self._enqueue_reply, channel_info=self._channel_info)
        self.stop_event = threading.Event()
        self.started_at = now()
        self._threads: list[threading.Thread] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._notify_history: dict[str, float] = {}
        self._audience = list(self.cfg.owner_ids)
        self._last_channel_error = ""

    # -- delivery --------------------------------------------------------
    def _channel_info(self) -> dict[str, Any]:
        """Live channel health for the /status reply."""
        return {
            "name": getattr(self.channel, "name", "unknown"),
            "healthy": self.channel.healthy(),
            "outbox": self.store.outbox_stats(),
        }

    def _enqueue_reply(self, text: str, kind: str = "reply") -> None:
        self.store.enqueue(text, kind=kind, dedupe=None)

    def push(
        self,
        text: str,
        *,
        kind: str = "notify",
        session_id: str | None = None,
        question_id: str | None = None,
        dedupe: str | None = None,
    ) -> int | None:
        return self.store.enqueue(text, kind=kind, dedupe=dedupe, session_id=session_id, question_id=question_id)

    def notify(
        self,
        session_id: str | None,
        title: str,
        body: str = "",
        kind: str = "info",
        dedupe: str | None = None,
    ) -> bool:
        """Throttled, deduped notification push."""
        sess = self.store.get_session(session_id) if session_id else None
        throttle_key = f"{session_id or '-'}:{kind}"
        ts = now()
        if kind in {"progress", "info"}:
            last = self._notify_history.get(throttle_key, 0.0)
            if ts - last < self.cfg.notify_min_interval_s:
                self.store.audit("notify_throttled", "daemon", {"session_id": session_id, "kind": kind})
                return False
        self._notify_history[throttle_key] = ts
        text = T.format_notify(kind, sess, title, body)
        # Dedupe on the *content*, not just the title: hooks can fire twice for
        # one event, but two genuinely different messages that happen to share a
        # title must both go out.
        digest = hashlib.sha1(f"{title}\x00{body}".encode("utf-8")).hexdigest()[:12]
        key = dedupe or f"n:{session_id}:{kind}:{int(ts // 10)}:{digest}"
        row = self.store.enqueue(text, kind="notify", dedupe=key, session_id=session_id)
        return row is not None

    # -- questions -------------------------------------------------------
    def ask(
        self,
        *,
        session_id: str | None,
        prompt: str,
        options: list[str] | None = None,
        timeout_s: int | None = None,
        dangerous: bool = False,
        default_action: str | None = None,
        context: dict[str, Any] | None = None,
        kind: str = "decision",
    ) -> Question:
        ttl = int(timeout_s or self.cfg.ask_timeout_s)
        if session_id:
            self.store.touch_session(session_id, "waiting")
        q = self.store.create_question(
            session_id=session_id or "",
            prompt=prompt,
            options=options,
            kind=kind,
            dangerous=dangerous,
            ttl_s=ttl,
            default_action=default_action,
            context=context,
        )
        sess = self.store.get_session(q.session_id) if q.session_id else None
        self.push(
            T.format_question(q, sess),
            kind="question",
            session_id=q.session_id,
            question_id=q.id,
            dedupe=f"q:{q.id}:v1",
        )
        self.store.audit(
            "question_created",
            "api",
            {"code": q.code, "session_id": q.session_id, "dangerous": dangerous, "ttl": ttl},
        )
        return q

    def wait_for(self, question_id: str, timeout: float) -> Question | None:
        deadline = now() + max(0.0, timeout)
        while True:
            q = self.store.get_question(question_id)
            if q is None:
                return None
            if q.state != "pending":
                return q
            if now() >= deadline or self.stop_event.is_set():
                return q
            time.sleep(0.2)

    # -- inbound ---------------------------------------------------------
    def handle_message(self, msg: InboundMessage) -> str:
        # Central inbound dedupe: a re-delivered message (long-poll replay after
        # reconnect, WeChat resend) must never apply a second answer.
        if not self.store.record_inbound(msg.msg_id, msg.from_user, msg.text, msg.ref_svr_id, msg.context_token):
            self.store.audit("inbound_duplicate", "daemon", {"msg_id": msg.msg_id})
            return "duplicate"
        try:
            outcome = self.router.handle_inbound(msg)
            self.store.mark_inbound(msg.msg_id, outcome)
            return outcome
        except Exception as exc:  # pragma: no cover - defensive
            self.store.audit("router_error", "daemon", {"error": str(exc), "msg": one_line(msg.text, 100)})
            log_stderr("[router] error: " + traceback.format_exc())
            return "error"

    # -- loops -----------------------------------------------------------
    def _channel_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.channel.start(self.handle_message, self.stop_event)
            except ChannelError as exc:
                self._last_channel_error = str(exc)
                self.store.audit("channel_start_failed", "daemon", {"error": str(exc)})
                log_stderr(f"[channel] {exc}")
            except Exception as exc:  # pragma: no cover
                self._last_channel_error = str(exc)
                log_stderr(f"[channel] crashed: {exc}\n{traceback.format_exc()}")

            if self.stop_event.is_set():
                return
            # The channel returned on its own (most often: WeChat token expired,
            # errcode -14). Never silently run without a channel: keep the daemon
            # alive, stay visibly unhealthy, and retry - a later `arb login`
            # rewrites the credentials and the next attempt picks them up.
            self._last_channel_error = getattr(self.channel, "auth_error", "") or self._last_channel_error
            self.store.audit("channel_restart_pending", "daemon", {"error": self._last_channel_error})
            log_stderr(
                "[channel] stopped (%s); retrying in 30s. If this repeats, run `arb login`."
                % (self._last_channel_error or "no detail")
            )
            self.stop_event.wait(30)

    def _outbox_loop(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            rows = self.store.due_outbox(5)
            if not rows:
                self.stop_event.wait(0.25)
                continue
            if not self.channel.healthy():
                # holding is correct: never drop a decision notification, and
                # never treat a delivery failure as an answer.
                self.stop_event.wait(min(5.0, backoff))
                backoff = min(30.0, backoff * 1.5)
                continue
            for row in rows:
                if self.stop_event.is_set():
                    return
                try:
                    msg_id = ""
                    for chunk in chunk_text(row["text"], self.cfg.max_push_chars):
                        msg_id = self.channel.send(chunk)
                    self.store.mark_outbox_sent(row["id"], msg_id)
                    if row["question_id"]:
                        self.store.mark_delivered(row["question_id"], msg_id)
                    backoff = 1.0
                except ChannelError as exc:
                    self._last_channel_error = str(exc)
                    retry = min(float(self.cfg.outbox_retry_max_s), max(2.0, backoff))
                    self.store.mark_outbox_failed(row["id"], str(exc), retry)
                    self.store.audit(
                        "delivery_failed",
                        "daemon",
                        {"outbox_id": row["id"], "kind": row["kind"], "error": str(exc), "retry_in": retry},
                    )
                    backoff = min(60.0, backoff * 2)
                    if exc.auth_invalid:
                        break
                except Exception as exc:  # pragma: no cover
                    self.store.mark_outbox_failed(row["id"], str(exc), 30.0)
                    log_stderr(f"[outbox] unexpected: {exc}")

    def _sweeper_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                for q in self.store.expire_due():
                    sess = self.store.get_session(q.session_id)
                    default = q.default_action or ("拒绝" if q.dangerous else "不做处理")
                    self.push(
                        f"⏰ #{q.code} 超时未回复，已按「{default}」处理。\n"
                        f"项目: {T.session_line(sess)}\n问题: {one_line(q.prompt, 120)}",
                        kind="expired",
                        session_id=q.session_id,
                        dedupe=f"expired:{q.id}",
                    )
                    self.store.audit("question_expired", "daemon", {"code": q.code, "dangerous": q.dangerous})
                    if q.session_id:
                        self.store.touch_session(q.session_id, "active")
            except Exception as exc:  # pragma: no cover
                log_stderr(f"[sweeper] {exc}")
            self.stop_event.wait(2.0)

    # -- recovery --------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        info: dict[str, Any] = {"expired": 0, "pending": 0, "unsent": 0}
        for _q in self.store.expire_due():
            info["expired"] += 1
        pending = self.store.pending_questions()
        info["pending"] = len(pending)
        stats = self.store.outbox_stats()
        info["unsent"] = stats["pending"]
        if pending:
            sess_map = {q.session_id: self.store.get_session(q.session_id) for q in pending}
            self.push(
                f"♻️ 桥接已重启，仍有 {len(pending)} 个问题等你回复：\n"
                + "\n".join(
                    f"#{q.code} [{T.session_line(sess_map.get(q.session_id))}] {one_line(q.prompt, 60)}"
                    for q in pending
                ),
                kind="recover",
                dedupe=f"recover:{int(now() // 60)}",
            )
        self.store.audit("recover", "daemon", info)
        return info

    # -- http ------------------------------------------------------------
    def status_payload(self) -> dict[str, Any]:
        healthy = self.channel.healthy()
        uptime = int(now() - self.started_at)
        # A just-started channel has not completed a long poll yet; calling that
        # "down" would be scary and wrong.
        if healthy:
            state = "healthy"
        elif uptime < 60 and not (self._last_channel_error or getattr(self.channel, "auth_error", "")):
            state = "starting"
        else:
            state = "down"
        return {
            "ok": True,
            "version": VERSION,
            "uptime_s": uptime,
            "channel": {
                "name": getattr(self.channel, "name", "unknown"),
                "healthy": healthy,
                "state": state,
                "last_error": self._last_channel_error,
                "auth_error": getattr(self.channel, "auth_error", ""),
                "logged_in": load_ilink(self.cfg).valid,
            },
            "stats": self.store.stats(),
            "outbox": self.store.outbox_stats(),
            "muted": self.router.muted,
            "approval_mode": self.cfg.approval_mode,
            "owner_ids": list(self.cfg.owner_ids),
        }

    def start_http(self) -> ThreadingHTTPServer:
        daemon = self
        # Never serve with an empty token: the auth check compares strings, so an
        # empty configured token would accept a request that omits the header.
        if not self.cfg.api_token:
            self.cfg.bootstrap()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = f"arb/{VERSION}"

            def log_message(self, fmt: str, *args: Any) -> None:  # silence per-request noise
                return

            # -- helpers --
            def _json(self, code: int, payload: Any) -> None:
                body = json_dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _auth_ok(self) -> bool:
                expected = daemon.cfg.api_token
                if not expected:
                    self._json(401, {"ok": False, "error": "daemon has no api token"})
                    return False
                token = self.headers.get("X-ARB-Token", "")
                if secret_compare(token, expected):
                    return True
                self._json(401, {"ok": False, "error": "bad token"})
                return False

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                return json_loads(raw, {}) or {}

            def do_GET(self) -> None:  # noqa: N802
                if not self._auth_ok():
                    return
                url = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(url.query).items()}
                try:
                    payload = daemon.handle_get(url.path, qs)
                except KeyError as exc:
                    self._json(404, {"ok": False, "error": f"not found: {exc}"})
                    return
                except Exception as exc:
                    self._json(500, {"ok": False, "error": str(exc)})
                    return
                self._json(200, payload)

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth_ok():
                    return
                url = urlparse(self.path)
                try:
                    body = self._body()
                    payload = daemon.handle_post(url.path, body)
                except KeyError as exc:
                    self._json(404, {"ok": False, "error": f"not found: {exc}"})
                    return
                except ValueError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    self._json(500, {"ok": False, "error": str(exc)})
                    return
                self._json(200, payload)

        self._httpd = _LoopbackServer(("127.0.0.1", self.cfg.port), Handler)
        self._httpd.daemon_threads = True
        return self._httpd

    def handle_get(self, path: str, qs: dict[str, str]) -> dict[str, Any]:
        if path == "/api/health":
            return self.status_payload()
        if path == "/api/status":
            return self.status_payload()
        if path == "/api/pending":
            pending = self.store.pending_questions()
            return {"ok": True, "pending": [q.to_dict() for q in pending]}
        if path == "/api/sessions":
            return {"ok": True, "sessions": self.store.list_sessions(include_stale=True)}
        if path == "/api/question":
            q = None
            if qs.get("id"):
                q = self.store.get_question(qs["id"])
            elif qs.get("code"):
                q = self.store.get_question_by_code(qs["code"])
            if q is None:
                raise KeyError("question")
            return {"ok": True, "question": q.to_dict()}
        if path == "/api/wait":
            qid = qs.get("id") or ""
            timeout = float(qs.get("timeout") or 30)
            if not qid:
                raise ValueError("id required")
            q = self.wait_for(qid, timeout)
            if q is None:
                raise KeyError("question")
            sess = self.store.get_session(q.session_id) if q.session_id else None
            return {"ok": True, "question": q.to_dict(), "session": sess}
        raise KeyError(path)

    def handle_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/session":
            sess = self.resolve_session(body)
            return {"ok": True, "session": sess}
        if path == "/api/session/close":
            sid = body.get("session_id") or ""
            if sid:
                self.store.touch_session(sid, "done")
            return {"ok": True}
        if path == "/api/notify":
            sess = self.resolve_session(body)
            ok = self.notify(
                sess["id"],
                title=str(body.get("title") or ""),
                body=str(body.get("body") or ""),
                kind=str(body.get("kind") or "info"),
                dedupe=body.get("dedupe"),
            )
            return {"ok": True, "queued": ok, "session": sess}
        if path == "/api/ask":
            sess = self.resolve_session(body)
            q = self.ask(
                session_id=sess["id"],
                prompt=str(body.get("prompt") or "").strip(),
                options=[str(o) for o in (body.get("options") or [])],
                timeout_s=int(body["timeout_s"]) if body.get("timeout_s") else None,
                dangerous=bool(body.get("dangerous")),
                default_action=body.get("default_action"),
                context=body.get("context") or {},
                kind=str(body.get("kind") or "decision"),
            )
            return {"ok": True, "question": q.to_dict(), "session": sess}
        if path == "/api/shutdown":
            threading.Thread(target=self._delayed_shutdown, daemon=True).start()
            return {"ok": True, "stopping": True}
        if path == "/api/answer":
            code = str(body.get("code") or "")
            caller = str(body.get("by") or "cli")
            # The HTTP caller is authenticated by the API token, so it is a
            # trusted local actor; keep its label for the audit trail but never
            # let it impersonate a WeChat sender (or dodge the allowlist).
            msg = InboundMessage(
                msg_id=new_id("local"),
                from_user=f"local-cli:{caller}",
                text=f"#{code} {body.get('text', '')}".strip(),
            )
            outcome = self.handle_message(msg)
            return {"ok": True, "outcome": outcome}
        raise KeyError(path)

    def resolve_session(self, body: dict[str, Any]) -> dict[str, Any]:
        sid = str(body.get("session_id") or "")
        if sid:
            sess = self.store.get_session(sid)
            if sess:
                self.store.touch_session(sid, body.get("status") or "active")
                return self.store.get_session(sid) or sess
        agent = str(body.get("agent") or "generic")
        cwd = str(body.get("cwd") or os.getcwd())
        project = str(body.get("project") or os.path.basename(cwd.rstrip("\\/")) or "project")
        key = body.get("key")
        return self.store.upsert_session(
            agent=agent,
            project=project,
            cwd=cwd,
            label=str(body.get("label") or ""),
            parent_id=body.get("parent_id"),
            meta=body.get("meta") or {},
            session_id=sid or None,
            key=str(key) if key else None,
            status=str(body.get("status") or "active"),
        )

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._channel_loop, name="channel", daemon=True),
            threading.Thread(target=self._outbox_loop, name="outbox", daemon=True),
            threading.Thread(target=self._sweeper_loop, name="sweeper", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def serve(self) -> None:
        httpd = self.start_http()
        self.start()
        self.recover()
        self.store.audit("daemon_start", "daemon", {"port": self.cfg.port, "channel": getattr(self.channel, "name", "?")})
        log_stderr(f"[arb] listening on http://127.0.0.1:{self.cfg.port}  channel={getattr(self.channel, 'name', '?')}")
        try:
            httpd.serve_forever(poll_interval=0.3)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.stop_event.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
        try:
            self.channel.close()
        except Exception:
            pass
        self.store.audit("daemon_stop", "daemon", {})

    def _delayed_shutdown(self) -> None:
        time.sleep(0.3)
        self.shutdown()


def run_daemon(cfg: Config | None = None) -> int:
    daemon = Daemon(cfg)
    try:
        daemon.serve()
    except KeyboardInterrupt:
        daemon.shutdown()
    except OSError as exc:
        log_stderr(f"[arb] cannot start: {exc}")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_daemon())

"""Inbound routing: turn one WeChat message into exactly one correct action.

Routing precedence (highest first). Every step is deliberately conservative:
when the target is ambiguous we ask, we never guess.

1. ``/pair <code>``          - first-run binding of the owner's WeChat id.
2. ``/<command>``            - status/pending/approve/deny/cancel/...
3. quoted message (ref_msg)  - WeChat "引用回复" pins the exact pushed question.
4. explicit ``#CODE``        - unambiguous reference to one question.
5. exactly one pending       - bare text answers it (dangerous needs explicit ok).
6. several pending           - refused, with a numbered list to reply to.

Anything else is ignored. A question can transition pending -> answered at most
once, enforced by a conditional UPDATE in the store.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import text as T
from .channel.base import InboundMessage
from .config import Config
from .store import Question, Store
from .util import iso, one_line

Sender = Callable[[str, str], None]  # (text, kind) -> enqueue for delivery

# Loopback/API callers are already authenticated by the API token; they are not
# remote humans and must not be filtered by the WeChat allowlist.
LOCAL_SENDERS = {"local-cli", "cli", "local", "hook"}


class Router:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        send: Sender,
        channel_info: Callable[[], dict[str, Any]] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.send = send
        self._channel_info = channel_info
        self._muted = False
        self._lock = threading.Lock()
        self._unauth_notified: dict[str, float] = {}

    # -- helpers ---------------------------------------------------------
    def _sessions(self, questions: list[Question]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for q in questions:
            sess = self.store.get_session(q.session_id)
            if sess:
                out[q.session_id] = sess
        return out

    def _session_of(self, q: Question) -> dict[str, Any] | None:
        return self.store.get_session(q.session_id)

    def _reply(self, text: str, kind: str = "reply") -> None:
        self.send(text, kind)

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, value: bool) -> None:
        self._muted = value
        self.store.audit("mute" if value else "unmute", "owner")

    # -- entry point -----------------------------------------------------
    def handle_inbound(self, msg: InboundMessage) -> str:
        raw = (msg.text or "").strip()
        if not raw:
            return "empty"

        # 1) owner binding ------------------------------------------------
        cmd = T.parse_command(raw)
        if cmd and cmd[0] == "pair":
            return self._try_pair(msg, cmd[1])

        if msg.from_user not in self.cfg.owner_ids and msg.from_user not in LOCAL_SENDERS:
            return self._reject_unknown(msg)

        # 2) commands -----------------------------------------------------
        if cmd:
            name, args = cmd
            handler = getattr(self, f"_cmd_{name}", None)
            if handler is not None:
                return handler(msg, args)
            # unknown command: fall through and treat it as a reply attempt
            # (people do type "/1" out of habit)

        if self._muted and not (cmd and cmd[0] in {"unmute", "status"}):
            return "muted"

        # 3) quoted reply -------------------------------------------------
        if msg.ref_svr_id:
            q = self.store.get_question_by_outbound(msg.ref_svr_id)
            if q is not None:
                return self._answer(q, raw, msg.from_user, explicit=True, source="quote")

        # 4) explicit #CODE ----------------------------------------------
        code, rest = T.extract_code(raw)
        if code:
            q = self.store.get_question_by_code(code)
            if q is None:
                self._reply(f"没有找到 #{code} 对应的问题。发送 /pending 查看当前待处理。")
                return "unknown_code"
            if not rest and q.options:
                self._reply(f"请在 #{code} 后面写上答复，例如: #{code} 1")
                return "need_answer_text"
            return self._answer(q, rest or raw, msg.from_user, explicit=True, source="code")

        # 5/6) fall back to pending count ---------------------------------
        pending = self.store.pending_questions()
        if not pending:
            self._reply(T.format_no_pending())
            return "no_pending"
        if len(pending) == 1:
            q = pending[0]
            if q.dangerous and not T.is_explicit_approval(raw):
                self._reply(
                    f"⚠️ #{q.code} 是危险操作，为防误批，必须明确批准：\n"
                    f"回复 `/approve {q.code}` 或直接回复 `yes`；拒绝请回复 `/deny {q.code}`。"
                )
                return "dangerous_needs_explicit"
            return self._answer(q, raw, msg.from_user, explicit=False, source="sole_pending")
        self._reply(T.format_ambiguous(pending, self._sessions(pending)))
        return "ambiguous"

    # -- binding ---------------------------------------------------------
    def _try_pair(self, msg: InboundMessage, supplied: str) -> str:
        supplied = (supplied or "").strip().upper()
        expected = (self.cfg.pairing_code or "").upper()
        if not expected or supplied != expected:
            self.store.audit("pair_failed", msg.from_user, {"supplied_len": len(supplied)})
            self._reply("绑定码不正确。请在本机运行 `arb status` 查看当前绑定码。")
            return "pair_failed"
        if msg.from_user not in self.cfg.owner_ids:
            self.cfg.owner_ids.append(msg.from_user)
            self.cfg.save()
        self.store.audit("pair_ok", msg.from_user, {"owner_ids": self.cfg.owner_ids})
        self._reply(
            "✅ 绑定成功，这台机器上的 Agent 现在可以向你推送消息了。\n"
            "发送 /help 查看可用命令；发送 /status 查看当前状态。"
        )
        return "paired"

    def _reject_unknown(self, msg: InboundMessage) -> str:
        now_ts = time.time()
        last = self._unauth_notified.get(msg.from_user, 0.0)
        self.store.audit("unauthorized_inbound", msg.from_user, {"text": one_line(msg.text, 120)})
        if now_ts - last > 3600:
            self._unauth_notified[msg.from_user] = now_ts
            # never echo the pairing code to an unknown sender
            self.store.enqueue(
                "⚠️ 收到未授权微信号的消息，已忽略。\n"
                f"来源ID: {one_line(msg.from_user, 24)}\n"
                f"内容: {one_line(msg.text, 80)}\n"
                "若这是你的另一个设备，请用 /pair 绑定。",
                kind="security",
                dedupe=f"unauth:{msg.from_user}:{int(now_ts // 3600)}",
            )
        self._reply("⛔ 未授权的发送者，消息已忽略。")
        return "unauthorized"

    # -- answering -------------------------------------------------------
    def _answer(self, q: Question, raw: str, by: str, *, explicit: bool, source: str) -> str:
        if q.state != "pending":
            sess = self._session_of(q)
            self._reply(T.format_already_handled(q, sess))
            return f"not_pending:{q.state}"

        answer = T.normalize_choice(raw, q.options)
        if answer is None:
            opts = "、".join(f"{i}){o}" for i, o in enumerate(q.options or [], 1))
            self._reply(f"没看懂「{one_line(raw, 30)}」对应的选项。\n#{q.code} 的选项: {opts or '(自由文本)'}")
            return "unparsed"

        # Dangerous operations require an unambiguous approval gesture: either
        # the /approve|/deny command, or an explicit "yes"/"允许" reply. A bare
        # "1" — even with the right #CODE — is not enough. This is the rule that
        # keeps a mistyped reply from running `rm -rf`.
        if q.dangerous and not (source == "verdict" or T.is_explicit_approval(raw)):
            self._reply(f"⚠️ #{q.code} 是危险操作：请用 /approve {q.code} 或 /deny {q.code} 明确处理。")
            return "dangerous_needs_explicit"

        won, fresh = self.store.claim_answer(q.id, answer, raw, by)
        sess = self._session_of(fresh or q)
        if not won:
            self._reply(T.format_already_handled(fresh or q, sess))
            self.store.audit("answer_rejected_race", by, {"code": q.code, "raw": one_line(raw, 80)})
            return "lost_race"
        self.store.audit("answer", by, {"code": q.code, "answer": answer, "source": source, "raw": one_line(raw, 120)})
        self._reply(T.format_confirmation(fresh or q, sess, answer))
        return f"answered:{q.code}"

    # -- commands --------------------------------------------------------
    def _cmd_help(self, msg: InboundMessage, args: str) -> str:
        self._reply(T.HELP_TEXT)
        return "help"

    def _cmd_status(self, msg: InboundMessage, args: str) -> str:
        stats = self.store.stats()
        if self._channel_info is not None:
            try:
                channel = self._channel_info()
            except Exception:  # pragma: no cover - status must never break
                channel = {"name": "unknown", "healthy": False}
        else:
            channel = {"name": "bridge", "healthy": True, "outbox": self.store.outbox_stats()}
        channel.setdefault("outbox", self.store.outbox_stats())
        self._reply(T.format_status(stats, self.store.list_sessions(), channel))
        return "status"

    def _cmd_pending(self, msg: InboundMessage, args: str) -> str:
        pending = self.store.pending_questions()
        self._reply(T.format_pending_list(pending, self._sessions(pending)))
        return "pending"

    def _cmd_projects(self, msg: InboundMessage, args: str) -> str:
        sessions = self.store.list_sessions(limit=15)
        if not sessions:
            self._reply("还没有登记任何会话。")
            return "projects"
        lines = ["🗂 会话列表:"]
        for s in sessions:
            lines.append(f" · {T.session_line(s)} [{s.get('status')}] {iso(s.get('last_seen'))[11:16]}")
        self._reply("\n".join(lines))
        return "projects"

    def _cmd_answer(self, msg: InboundMessage, args: str) -> str:
        code, rest = T.arg_code(args)
        if not code:
            self._reply("用法: /answer #CODE 你的答复")
            return "usage"
        q = self.store.get_question_by_code(code)
        if q is None:
            self._reply(f"没有找到 #{code}。发送 /pending 查看。")
            return "unknown_code"
        return self._answer(q, rest, msg.from_user, explicit=True, source="command")

    def _cmd_approve(self, msg: InboundMessage, args: str) -> str:
        return self._verdict(msg, args, approve=True)

    def _cmd_deny(self, msg: InboundMessage, args: str) -> str:
        return self._verdict(msg, args, approve=False)

    def _verdict(self, msg: InboundMessage, args: str, *, approve: bool) -> str:
        code, _rest = T.arg_code(args)
        if not code:
            pending = self.store.pending_questions()
            if len(pending) == 1:
                q = pending[0]
            else:
                self._reply(
                    T.format_ambiguous(pending, self._sessions(pending)) if pending else T.format_no_pending()
                )
                return "ambiguous"
        else:
            q = self.store.get_question_by_code(code)
            if q is None:
                self._reply(f"没有找到 #{code}。")
                return "unknown_code"

        if q.options:
            wanted = "allow" if approve else "deny"
            pick = None
            for opt in q.options:
                if opt.strip().lower() == wanted:
                    pick = opt
                    break
            if pick is None:
                pick = q.options[0] if approve else (q.options[1] if len(q.options) > 1 else q.options[0])
        else:
            pick = "allow" if approve else "deny"
        return self._answer(q, pick, msg.from_user, explicit=True, source="verdict")

    def _cmd_cancel(self, msg: InboundMessage, args: str) -> str:
        code, _ = T.arg_code(args)
        if not code:
            self._reply("用法: /cancel #CODE")
            return "usage"
        q = self.store.get_question_by_code(code)
        if q is None:
            self._reply(f"没有找到 #{code}。")
            return "unknown_code"
        if self.store.set_question_state(q.id, "cancelled", note="cancelled by owner"):
            self.store.audit("cancel", msg.from_user, {"code": code})
            self._reply(f"🚫 #{code} 已取消，Agent 会按默认方式处理。")
            return "cancelled"
        self._reply(f"#{code} 已经不是待处理状态。")
        return "not_pending"

    def _cmd_mute(self, msg: InboundMessage, args: str) -> str:
        self.set_muted(True)
        self._reply("🔕 已静音：只处理命令，不推送普通通知。")
        return "muted"

    def _cmd_unmute(self, msg: InboundMessage, args: str) -> str:
        self.set_muted(False)
        self._reply("🔔 已恢复通知。")
        return "unmuted"

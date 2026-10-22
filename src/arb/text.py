"""Message rendering and reply parsing.

All human-facing strings live here so the wording can be tuned without touching
routing logic, and so tests can assert on exact formats.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .store import Question
from .util import humanize_duration, one_line, truncate

CODE_RE = re.compile(r"#\s*([2-9A-HJ-NP-Z]{4})\b", re.I)
CODE_LEAD_RE = re.compile(r"^\s*([2-9A-HJ-NP-Z]{4})\s*[:：]\s*(.+)$", re.I)
CMD_RE = re.compile(r"^\s*/\s*([a-zA-Z_\u4e00-\u9fff]+)\s*(.*)$", re.S)

AFFIRM = {
    "y", "yes", "yep", "ok", "okay", "allow", "approve", "approved",
    "同意", "允许", "可以", "好", "行", "确认", "是", "1",
}
DENY = {
    "n", "no", "deny", "denied", "reject", "block",
    "拒绝", "不允许", "不要", "否", "取消", "0",
}

# Chinese/English ordinal words used for multiple choice
_ORDINAL_WORDS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "first": 0, "second": 1, "third": 2,
}


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------
def session_line(session: dict[str, Any] | None) -> str:
    if not session:
        return "未知会话"
    project = session.get("project") or "?"
    agent = session.get("agent") or "?"
    label = session.get("label") or project
    if label and label != project:
        return f"{project} · {label} ({agent})"
    return f"{project} ({agent})"


def format_question(q: Question, session: dict[str, Any] | None, *, ttl_hint: int | None = None) -> str:
    lines = [f"🚨 需要你决策  #{q.code}", f"项目: {session_line(session)}", f"问题: {one_line(q.prompt, 400)}"]
    if q.options:
        lines.append("选项:")
        for i, opt in enumerate(q.options, start=1):
            lines.append(f" {i}) {one_line(opt, 60)}")
        hint = "直接回复序号"
    else:
        hint = "直接回复内容"
    lines.append(f'回复: {hint}，或回 "#{q.code} 你的答复"')
    if q.dangerous:
        lines.append(f"⚠️ 危险操作：必须明确批准 —— 回复 yes 或 /approve {q.code}")
    left = ttl_hint if ttl_hint is not None else int(q.remaining)
    default = q.default_action or ("拒绝" if q.dangerous else "不做处理")
    lines.append(f"超时: {humanize_duration(left)}后自动按「{default}」处理")
    return "\n".join(lines)


def format_confirmation(q: Question, session: dict[str, Any] | None, answer: str) -> str:
    return f"✅ 已收到 #{q.code} → {one_line(answer, 80)}\n已转交 {session_line(session)} 继续执行。"


def format_already_handled(q: Question, session: dict[str, Any] | None) -> str:
    if q.state == "answered":
        return f"⚠️ #{q.code} 已经处理过了（答复: {one_line(q.answer or '', 40)}），本次回复已忽略。"
    if q.state == "expired":
        return f"⚠️ #{q.code} 已超时失效，本次回复已忽略。{session_line(session)} 不会收到该答复。"
    return f"⚠️ #{q.code} 当前状态为 {q.state}，本次回复已忽略。"


def format_notify(kind: str, session: dict[str, Any] | None, title: str, body: str = "", extra: str = "") -> str:
    icon = {
        "start": "🟢",
        "progress": "⏳",
        "done": "✅",
        "error": "❌",
        "attention": "🔔",
        "info": "ℹ️",
    }.get(kind, "ℹ️")
    head = f"{icon} {title}" if title else f"{icon} {kind}"
    parts = [head, f"项目: {session_line(session)}"]
    if body:
        parts.append(truncate(body.strip(), 900))
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def format_pending_list(questions: Iterable[Question], sessions: dict[str, dict[str, Any]]) -> str:
    items = list(questions)
    if not items:
        return "当前没有待处理的问题。"
    lines = [f"待处理问题 {len(items)} 个:"]
    for q in items:
        sess = sessions.get(q.session_id)
        lines.append(f"#{q.code} [{session_line(sess)}] {one_line(q.prompt, 60)} (剩 {humanize_duration(q.remaining)})")
    lines.append("回复: #代码 你的答复")
    return "\n".join(lines)


def format_status(stats: dict[str, Any], sessions: list[dict[str, Any]], channel: dict[str, Any]) -> str:
    lines = [
        "📊 Agent Remote Bridge",
        f"通道: {channel.get('name')} / {'在线' if channel.get('healthy') else '离线'}",
        f"会话: {stats.get('active_sessions', 0)} 活跃 / {stats.get('sessions', 0)} 总计",
        f"问题: {stats.get('pending_questions', 0)} 待处理 / {stats.get('answered', 0)} 已回答 / {stats.get('expired', 0)} 超时",
        f"待发消息: {(channel.get('outbox') or {}).get('pending', 0)}",
    ]
    if sessions:
        lines.append("最近会话:")
        for s in sessions[:5]:
            lines.append(f" · {session_line(s)} [{s.get('status')}]")
    return "\n".join(lines)


def format_ambiguous(questions: list[Question], sessions: dict[str, dict[str, Any]]) -> str:
    lines = [f"有 {len(questions)} 个问题正在等待，为避免串线，请带上编号回复："]
    for q in questions:
        lines.append(f"#{q.code} [{session_line(sessions.get(q.session_id))}] {one_line(q.prompt, 50)}")
    lines.append("示例: #" + questions[0].code + " 你的答复")
    return "\n".join(lines)


def format_no_pending() -> str:
    return "当前没有等待回复的问题。发送 /status 查看概况，/help 查看命令。"


def format_unknown_sender(from_user: str, pairing_code: str) -> str:
    return (
        "👋 这是一个私有桥接机器人，当前微信号未授权。\n"
        f"如确认是你本人，请回复: /pair {pairing_code}\n"
        f"(来源: {one_line(from_user, 20)})"
    )


HELP_TEXT = """🤖 Agent Remote Bridge 命令
/status            运行状态
/pending           列出待处理问题
/answer #CODE 内容  回复某个问题
/approve #CODE     批准（危险操作必须用这个，或回复 yes）
/deny #CODE        拒绝
/cancel #CODE      取消该问题（让 Agent 按默认处理）
/projects          查看会话列表
/mute /unmute      暂停/恢复通知推送
/help              本帮助
其余文字：若只有一个待处理问题就直接作为答复；多个时请带 #CODE。"""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def parse_command(text: str) -> tuple[str, str] | None:
    m = CMD_RE.match(text or "")
    if not m:
        return None
    return m.group(1).lower(), m.group(2).strip()


def extract_code(text: str) -> tuple[str | None, str]:
    """Find an explicit question code. Returns (code, remaining_text)."""
    raw = (text or "").strip()
    lead = CODE_LEAD_RE.match(raw)
    if lead:
        return lead.group(1).upper(), lead.group(2).strip()
    m = CODE_RE.search(raw)
    if m:
        rest = (raw[: m.start()] + raw[m.end():]).strip(" :：,，-")
        return m.group(1).upper(), rest
    return None, raw


ARG_CODE_RE = re.compile(r"^#?\s*([2-9A-HJ-NP-Z]{4})\b\s*[:：]?\s*(.*)$", re.I | re.S)


def arg_code(text: str) -> tuple[str | None, str]:
    """Parse a command argument that should start with a code.

    Accepts ``#AB12 1``, ``AB12 1`` and ``ab12:1`` — used by /answer, /approve,
    /deny and /cancel so people do not have to remember the ``#``.
    """
    raw = (text or "").strip()
    m = ARG_CODE_RE.match(raw)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return extract_code(raw)


def normalize_choice(text: str, options: list[str] | None) -> str | None:
    """Map a human reply onto one of the offered options.

    Returns the canonical option string, or the trimmed free text when no
    options were offered, or None when the reply cannot be mapped.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    opts = options or []
    if not opts:
        return raw

    low = raw.lower().strip("。.!！,， ")

    # 1) exact option text (case-insensitive) or unique prefix
    for opt in opts:
        if opt.strip().lower() == low:
            return opt
    matches = [opt for opt in opts if low and opt.strip().lower().startswith(low)]
    if len(matches) == 1:
        return matches[0]

    # 2) numeric index
    if re.fullmatch(r"\d+", low):
        idx = int(low) - 1
        if 0 <= idx < len(opts):
            return opts[idx]

    # 3) letter index (a/b/c)
    if re.fullmatch(r"[a-z]", low):
        idx = ord(low) - ord("a")
        if 0 <= idx < len(opts):
            return opts[idx]

    # 4) Chinese ordinal words
    if low in _ORDINAL_WORDS:
        idx = _ORDINAL_WORDS[low]
        if 0 <= idx < len(opts):
            return opts[idx]

    # 5) yes/no synonyms when the option set is recognisably boolean
    yes_idx = [i for i, o in enumerate(opts) if o.strip().lower() in {"yes", "allow", "approve", "允许", "同意"}]
    no_idx = [i for i, o in enumerate(opts) if o.strip().lower() in {"no", "deny", "reject", "拒绝", "不允许"}]
    if yes_idx and no_idx:
        if low in AFFIRM:
            return opts[yes_idx[0]]
        if low in DENY:
            return opts[no_idx[0]]
    return None


def is_explicit_approval(text: str) -> bool:
    low = (text or "").strip().lower().strip("。.!！")
    return low in {"yes", "allow", "approve", "approved", "允许", "同意", "批准"}

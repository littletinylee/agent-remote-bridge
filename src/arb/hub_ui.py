"""Web console: login, pending decisions, and a WeChat-like chat window.

Chat is deliberately read-only with respect to the bridge's power: a message typed
here goes to the same DeepSeek advisor that answers WeChat free text, and that
advisor cannot approve anything. If the text contains a `#CODE`, it is treated as
an answer and goes down the normal decision path instead (pushed to the machine,
which re-checks it locally).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import time
from typing import Any

MAX_ANSWER = 2000
COOKIE = "arb_ui"
DEFAULT_USER = "q045204"
DEFAULT_PASSWORD = "q045204"
CODE_RE = re.compile(r"#([2-9A-HJ-NP-Z]{4})\b", re.I)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _age(created: float | None) -> str:
    if not created:
        return "?"
    secs = int(time.time() - float(created))
    if secs < 60:
        return f"{secs} 秒前"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    return f"{secs // 3600} 小时前"


def _clock(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(float(ts)))


STYLE = """
 :root { color-scheme: light dark }
 body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 0 auto; padding: 16px; line-height: 1.5; max-width: 760px }
 h1 { font-size: 18px; margin: 0 0 12px }
 h2 { font-size: 15px; margin: 20px 0 8px; opacity: .8 }
 .q { border: 1px solid #8884; border-radius: 10px; padding: 12px; margin: 0 0 12px }
 .code { font-family: ui-monospace, Consolas, monospace; font-weight: 700 }
 .meta { opacity: .75; font-size: 13px }
 .prompt { white-space: pre-wrap; margin: 8px 0; font-size: 14px }
 form { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap }
 input { flex: 1; min-width: 180px; padding: 10px; font-size: 16px;
         border-radius: 8px; border: 1px solid #8886; box-sizing: border-box }
 button { padding: 10px 16px; font-size: 16px; border-radius: 8px; cursor: pointer }
 .ok { background: #1a7f3722; padding: 10px; border-radius: 8px; margin-bottom: 12px }
 .warn { background: #bb800922; padding: 10px; border-radius: 8px; margin-bottom: 12px }
 .empty { opacity: .7 }
 .log { border: 1px solid #8884; border-radius: 10px; padding: 10px;
        height: 46vh; overflow-y: auto; background: #8080800d }
 .row { display: flex; margin: 6px 0 }
 .row.me { justify-content: flex-end }
 .bubble { max-width: 78%; padding: 8px 12px; border-radius: 14px;
           white-space: pre-wrap; word-break: break-word; font-size: 14px }
 .bot .bubble { background: #3b82f622; border: 1px solid #3b82f64d }
 .me .bubble { background: #1a7f3733; border: 1px solid #1a7f3755 }
 .sys .bubble { background: #8881; font-size: 13px; opacity: .85 }
 .time { font-size: 11px; opacity: .55; margin: 0 6px; align-self: flex-end }
 footer { margin-top: 20px; font-size: 12px; opacity: .6 }
"""


def page(body: str, title: str = "Agent Remote Bridge", refresh: int = 5) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}
<title>{_esc(title)}</title>
<style>{STYLE}</style>
</head><body>
<h1>{_esc(title)}</h1>
{body}
<footer>每 5 秒自动刷新 · 决策答复会交给对应机器本地复验；聊天由只读顾问回答，不能批准任何操作</footer>
</body></html>
"""


def login_form(warning: str = "") -> str:
    head = f'<div class="warn">{_esc(warning)}</div>' if warning else ""
    return page(
        head
        + """<p>请登录。</p>
<form method="post" action="/ui">
  <input type="text" name="user" placeholder="账号" autocomplete="username" required>
  <input type="password" name="password" placeholder="密码" autocomplete="current-password" required>
  <button type="submit">登录</button>
</form>""",
        refresh=0,
    )


def _question_block(q: dict) -> str:
    return f"""<div class="q">
  <div><span class="code">#{_esc(q.get('code'))}</span>
       <span class="meta">[{_esc(q.get('node_name'))}] {_esc(_age(q.get('created_at')))}</span></div>
  <div class="prompt">{_esc((q.get('prompt') or '')[:900])}</div>
  <form method="post" action="/ui/answer">
    <input type="hidden" name="code" value="{_esc(q.get('code'))}">
    <input type="text" name="answer" placeholder="输入答复，例如 yes / allow / 你的意见" autocomplete="off" required>
    <button type="submit">答复</button>
  </form>
</div>"""


def _chat_block(messages: list[dict]) -> str:
    if not messages:
        body = '<p class="empty">还没有对话。问问进度，例如「现在几台机器在线？」「workbench 在干什么？」</p>'
    else:
        rows = []
        for m in messages:
            role = m.get("role") or "bot"
            rows.append(
                f'<div class="row {role}"><div class="bubble">{_esc(m.get("text"))}</div>'
                f'<span class="time">{_esc(_clock(m.get("ts")))}</span></div>'
            )
        body = "".join(rows)
    return f"""<h2>聊天</h2>
<div class="log">{body}</div>
<form method="post" action="/ui/chat">
  <input type="hidden" name="chat" value="1">
  <input type="text" name="text" placeholder="说点什么…（含 #编号 则按决策答复处理）" autocomplete="off" required>
  <button type="submit">发送</button>
</form>"""


def render(store: Any, message: str = "", warning: str = "") -> str:
    rows = []
    for q in store.pending():
        node = store.node_by_id(q["node_id"])
        rows.append({
            "code": q["code"],
            "prompt": q["prompt"],
            "created_at": q["created_at"],
            "node_name": node["name"] if node else q["node_id"],
        })
    head = ""
    if message:
        head += f'<div class="ok">{_esc(message)}</div>'
    if warning:
        head += f'<div class="warn">{_esc(warning)}</div>'

    if rows:
        decisions = '<h2>待你决策</h2>' + "".join(_question_block(q) for q in rows)
    else:
        decisions = '<p class="empty">当前没有待回答的问题。</p>'

    return page(head + decisions + _chat_block(store.chat_recent(60)))


def _session_value(user: str, password: str) -> str:
    return hashlib.sha256(f"{user}|{password}|arb-ui".encode()).hexdigest()


def handle_ui(
    store: Any, hub: Any, method: str, query: dict, form: dict, cookie: str = ""
) -> tuple[int, str, str]:
    """Returns (status, html, set_cookie_value)."""
    user = str(getattr(hub, "ui_user", "") or DEFAULT_USER)
    password = str(getattr(hub, "ui_password", "") or DEFAULT_PASSWORD)
    if not user or not password:
        return 404, page('<p class="warn">网页控制台未启用：请配置 <code>ui_user</code> / <code>ui_password</code>。</p>'), ""

    session = _session_value(user, password)
    set_cookie = f"{COOKIE}={session}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800"
    authorized = bool(cookie) and hmac.compare_digest(cookie, session)

    if method == "POST" and form.get("user") is not None:
        given_user = str(form.get("user") or "")
        given_pass = str(form.get("password") or "")
        if hmac.compare_digest(given_user, user) and hmac.compare_digest(given_pass, password):
            return 200, render(store), set_cookie
        return 403, login_form("账号或密码不正确。"), ""

    if not authorized:
        return 200, login_form(), ""

    # -- chat: free text, unless it carries a decision code ------------------
    if method == "POST" and form.get("chat"):
        text = str(form.get("text") or "").strip()[:MAX_ANSWER]
        if text:
            store.chat_add("me", text)
            m = CODE_RE.search(text)
            if m and store.resolve(m.group(1)) is not None:
                q = store.resolve(m.group(1))
                answer = (text[: m.start()] + text[m.end():]).strip(" :：,，-") or text
                store.push(q["node_id"], answer, kind="answer", code=q["code"], frm=(hub.owner_ids or ["web"])[0])
                store.audit("web-console", "answer_sent", {"code": q["code"], "via": "chat"})
                node = store.node_by_id(q["node_id"])
                store.chat_add("sys", f"已把答复交给 {node['name'] if node else q['node_id']}（#{q['code']}）。")
            else:
                advisor = getattr(hub, "chat", None)
                if advisor is None:
                    store.chat_add("sys", "聊天层未启用：中转站没有配置 deepseek_key_file。")
                else:
                    try:
                        reply = advisor.reply(text, node_names=[n["name"] for n in store.nodes()])
                    except Exception as exc:
                        reply = None
                        store.audit("web-console", "chat_error", {"error": str(exc)[:150]})
                    store.chat_add("bot", reply or "（顾问暂时没有回答）")
        return 200, render(store), set_cookie

    if method == "GET" or not str(form.get("code") or "").strip():
        return 200, render(store), set_cookie

    # -- answer a pending decision ------------------------------------------
    code = str(form.get("code") or "").strip().upper()
    answer = str(form.get("answer") or "").strip()[:MAX_ANSWER]
    if not code or not answer:
        return 400, render(store, warning="编号和答复都不能为空。"), set_cookie

    q = store.resolve(code)
    if q is None:
        return 404, render(store, warning=f"没有 #{code} 这个待决策项。"), set_cookie
    if q["state"] != "pending":
        return 409, render(store, warning=f"#{code} 已经处理过了（{q['state']}），未被采纳。"), set_cookie

    owner = (hub.owner_ids or ["web-console"])[0]
    store.push(q["node_id"], answer, kind="answer", code=code, frm=owner)
    store.audit("web-console", "answer_sent", {"code": code, "answer": answer[:80]})
    node = store.node_by_id(q["node_id"])
    name = node["name"] if node else q["node_id"]
    store.chat_add("sys", f"已把答复交给 {name}（#{code}）。")
    return 200, render(
        store, message=f"已把答复交给 {name}（#{code}）。它接受后会把 #{code} 关闭；刷新即可看到结果。"
    ), set_cookie

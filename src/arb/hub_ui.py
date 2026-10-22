"""Web console on the hub: see pending questions, answer them.

Auth is a username + password login (defaults come from hub.json); the session is
an HttpOnly cookie so the browser remembers it. The console is disabled entirely
if no credentials are set.

Safety model, unchanged: answering means "push this text into the node's inbox",
exactly like a WeChat reply. The node's own router still re-checks expiry, the
dangerous-operation gate and the once-only transition, and the hub never closes a
question itself - the node does, after it accepts the answer.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from typing import Any

MAX_ANSWER = 2000
COOKIE = "arb_ui"
DEFAULT_USER = "q045204"
DEFAULT_PASSWORD = "q045204"


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


def page(body: str, title: str = "Agent Remote Bridge", refresh: int = 5) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}
<title>{_esc(title)}</title>
<style>
 :root {{ color-scheme: light dark }}
 body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 0 auto; padding: 16px; line-height: 1.5; max-width: 720px }}
 h1 {{ font-size: 18px; margin: 0 0 12px }}
 .q {{ border: 1px solid #8884; border-radius: 10px; padding: 12px; margin: 0 0 12px }}
 .code {{ font-family: ui-monospace, Consolas, monospace; font-weight: 700 }}
 .meta {{ opacity: .75; font-size: 13px }}
 .prompt {{ white-space: pre-wrap; margin: 8px 0; font-size: 14px }}
 form {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap }}
 input {{ flex: 1; min-width: 200px; padding: 10px; font-size: 16px;
          border-radius: 8px; border: 1px solid #8886; box-sizing: border-box }}
 button {{ padding: 10px 16px; font-size: 16px; border-radius: 8px; cursor: pointer }}
 .ok {{ background: #1a7f3722; padding: 10px; border-radius: 8px; margin-bottom: 12px }}
 .warn {{ background: #bb800922; padding: 10px; border-radius: 8px; margin-bottom: 12px }}
 .empty {{ opacity: .7 }}
 footer {{ margin-top: 20px; font-size: 12px; opacity: .6 }}
</style>
</head><body>
<h1>{_esc(title)}</h1>
{body}
<footer>每 5 秒自动刷新 · 答复会像微信回复一样交给对应机器，由它本地复验后执行</footer>
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
    if not rows:
        body = head + '<p class="empty">当前没有待回答的问题。</p>'
    else:
        body = head + "".join(_question_block(q) for q in rows)
    return page(body)


def _session_value(user: str, password: str) -> str:
    """A cookie the browser can hold; it is not the password itself."""
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

    if method == "GET" or not str(form.get("code") or "").strip():
        return 200, render(store), set_cookie

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
    return 200, render(
        store, message=f"已把答复交给 {name}（#{code}）。它接受后会把 #{code} 关闭；刷新即可看到结果。"
    ), set_cookie

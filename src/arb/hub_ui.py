"""Web console: project status lines, their pending questions, then chat.

Layout (top to bottom), as asked:
  1. one line per project/machine with a status dot, its pending count and when
     it was last seen - each line is a link
  2. the pending questions of the selected line (or all of them), each with an
     answer box
  3. the chat window with the read-only advisor

Auth stays username/password from hub.json. Answering still pushes into the
machine's inbox with the owner's id, so the node's local router re-checks expiry,
the dangerous-operation gate and the once-only transition; the hub never closes a
question itself.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
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


def _ago(ts: float | None) -> str:
    if not ts:
        return "从未"
    secs = int(time.time() - float(ts))
    if secs < 60:
        return f"{secs} 秒前"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    return f"{secs // 86400} 天前"


def _clock(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(float(ts))) if ts else ""


STYLE = """
 :root { color-scheme: light dark }
 body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 0 auto; padding: 16px; line-height: 1.5; max-width: 780px }
 h1 { font-size: 18px; margin: 0 0 4px }
 h2 { font-size: 14px; margin: 18px 0 8px; opacity: .75; font-weight: 600 }
 .lines { border: 1px solid #8884; border-radius: 10px; overflow: hidden }
 a.line { display: flex; align-items: center; gap: 10px; padding: 11px 12px;
          text-decoration: none; color: inherit; border-bottom: 1px solid #8882 }
 a.line:last-child { border-bottom: 0 }
 a.line:hover { background: #8881 }
 a.line.sel { background: #3b82f61f }
 .dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 9px }
 .on { background: #1a7f37 } .off { background: #999 }
 .pname { font-weight: 600; font-size: 15px }
 .pmeta { margin-left: auto; font-size: 12px; opacity: .7; text-align: right }
 .badge { display: inline-block; padding: 1px 7px; border-radius: 9px;
          font-size: 12px; background: #bb800933; border: 1px solid #bb800966; margin-left: 6px }
 .none { padding: 12px; opacity: .7; font-size: 14px }
 .q { border: 1px solid #8884; border-radius: 10px; padding: 12px; margin: 0 0 10px }
 .code { font-family: ui-monospace, Consolas, monospace; font-weight: 700 }
 .meta { opacity: .75; font-size: 13px }
 .prompt { white-space: pre-wrap; margin: 8px 0; font-size: 14px }
 form { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap }
 input { flex: 1; min-width: 180px; padding: 10px; font-size: 16px;
         border-radius: 8px; border: 1px solid #8886; box-sizing: border-box }
 button { padding: 10px 16px; font-size: 16px; border-radius: 8px; cursor: pointer }
 .ok { background: #1a7f3722; padding: 10px; border-radius: 8px; margin-bottom: 12px }
 .warn { background: #bb800922; padding: 10px; border-radius: 8px; margin-bottom: 12px }
 .log { border: 1px solid #8884; border-radius: 10px; padding: 10px;
        height: 42vh; overflow-y: auto; background: #8080800d }
 .row { display: flex; margin: 6px 0 }
 .row.me { justify-content: flex-end }
 .bubble { max-width: 78%; padding: 8px 12px; border-radius: 14px;
           white-space: pre-wrap; word-break: break-word; font-size: 14px }
 .bot .bubble { background: #3b82f622; border: 1px solid #3b82f64d }
 .me .bubble { background: #1a7f3733; border: 1px solid #1a7f3755 }
 .sys .bubble { background: #8881; font-size: 13px; opacity: .85 }
 .time { font-size: 11px; opacity: .55; margin: 0 6px; align-self: flex-end }
 .all { font-size: 13px; opacity: .8; text-decoration: none; margin-left: 8px }
 footer { margin-top: 18px; font-size: 12px; opacity: .6 }
"""


def page(body: str, title: str = "Agent Remote Bridge", refresh: int = 4) -> str:
    script = ""
    if refresh:
        script = f"""<script>
(function () {{
  var KEY = "arbChatScroll";
  function typing() {{
    var el = document.activeElement;
    if (el && el.tagName === "INPUT" && (el.value || el.type === "password")) return true;
    return Array.prototype.some.call(document.querySelectorAll("input"), function (i) {{
      return i.dataset.dirty === "1";
    }});
  }}
  document.querySelectorAll("input").forEach(function (i) {{
    i.addEventListener("input", function () {{ i.dataset.dirty = "1"; }});
  }});
  document.querySelectorAll("form").forEach(function (f) {{
    f.addEventListener("submit", function () {{ sessionStorage.removeItem(KEY); }});
  }});
  var log = document.querySelector(".log");
  if (log) {{
    var saved = sessionStorage.getItem(KEY);
    log.scrollTop = saved ? Number(saved) : log.scrollHeight;
    log.addEventListener("scroll", function () {{ sessionStorage.setItem(KEY, String(log.scrollTop)); }});
  }}
  setInterval(function () {{ if (!typing()) location.reload(); }}, {int(refresh) * 1000});
}})();
</script>"""
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{STYLE}</style>
</head><body>
<h1>{_esc(title)}</h1>
{body}
<footer>自动刷新（输入时暂停） · 决策答复交给机器本地复验；聊天由只读顾问回答，不能批准任何操作</footer>
{script}
</body></html>
"""


def login_form(warning: str = "") -> str:
    head = f'<div class="warn">{_esc(warning)}</div>' if warning else ""
    return page(
        head
        + """<p>请登录。</p>
<form method="post" action="/">
  <input type="text" name="user" placeholder="账号" autocomplete="username" required>
  <input type="password" name="password" placeholder="密码" autocomplete="current-password" required>
  <button type="submit">登录</button>
</form>""",
        refresh=0,
    )


def _snapshot_of(node: dict) -> dict:
    snap = node.get("snapshot")
    if not isinstance(snap, str) or not snap:
        return {}
    try:
        obj = json.loads(snap)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _node_projects(node: dict) -> list[str]:
    obj = _snapshot_of(node)
    projects = obj.get("projects")
    if isinstance(projects, list):
        return [str(p) for p in projects if p][:6]
    return []


def project_rows(store: Any) -> list[dict]:
    """One row per project a machine reports; machines with nothing reported still get a row."""
    rows: list[dict] = []
    for node in store.nodes():
        node_pending = [q for q in store.pending() if q["node_id"] == node["id"]]
        snap = _snapshot_of(node)
        host = str(snap.get("host") or "").strip()
        projects = _node_projects(node) or [node["name"]]
        for project in projects:
            rows.append({
                "project": "" if project == node["name"] else project,
                "machine": node["name"],
                "online": bool(node.get("online")),
                "pending": len(node_pending),
                "codes": [q["code"] for q in node_pending][:4],
                "last_seen": node.get("last_seen"),
                "host": host,
                "since": snap.get("since"),
            })
    rows.sort(key=lambda r: (-r["pending"], not r["online"], r["project"]))
    return rows


def _usage_line(store: Any) -> str:
    try:
        u = store.usage_today()
    except Exception:
        return ""
    if not u.get("calls"):
        return '<div class="none">今日顾问消耗：0（还没有问过）</div>'
    total = u["prompt"] + u["completion"]
    return (
        f'<div class="none">今日顾问消耗：{total:,} tokens（{u["calls"]} 次调用；输入 {u["prompt"]:,} / 输出 {u["completion"]:,}） · 各项目的 token 由 Agent 自身统计，未上报</div>'
    )


def _span(since: Any) -> str:
    try:
        secs = int(time.time() - float(since))
    except (TypeError, ValueError):
        return "?"
    if secs < 60:
        return f"{secs} 秒"
    if secs < 3600:
        return f"{secs // 60} 分钟"
    if secs < 86400:
        return f"{secs // 3600} 小时 {secs % 3600 // 60} 分"
    return f"{secs // 86400} 天 {secs % 86400 // 3600} 小时"


def _lines(store: Any, selected: str) -> str:
    rows = project_rows(store)
    if not rows:
        return '<div class="lines"><div class="none">还没有机器注册。</div></div>'
    out = []
    for r in rows:
        key = r["machine"]
        sel = " sel" if key == selected else ""
        badge = f'<span class="badge">{r["pending"]} 待决策</span>' if r["pending"] else ""
        tokens = '<span class="pmeta">tokens —</span>'
        codes = f' #{r["codes"][0]}' if r["codes"] else ""
        out.append(
            f'<a class="line{sel}" href="/?m={_esc(key)}">'
            f'<span class="dot {"on" if r["online"] else "off"}"></span>'
            f'<span class="pname">{_esc(r["project"] or "(未上报项目)")}</span>'
            f'<span class="meta">{_esc(r["machine"])}{(" · " + _esc(r["host"])) if r["host"] and r["host"].lower() != r["machine"].lower() else ""}{_esc(codes)}</span>'
            f'<span class="meta">{("已运行 " + _esc(_span(r["since"]))) if r["since"] else ""}</span>'
            f'{badge}'
            f'{tokens}'
            f'<span class="pmeta">{_esc(_ago(r["last_seen"]))}</span>'
            f'</a>'
        )
    return f'<div class="lines">{"".join(out)}</div>'


def _questions(store: Any, selected: str) -> str:
    items = store.pending()
    if selected:
        node = store.node_by_name(selected)
        if node is not None:
            items = [q for q in items if q["node_id"] == node["id"]]
    if not items:
        return '<div class="none">这一行没有待回答的问题。</div>' if selected else \
               '<div class="none">当前没有待回答的问题。</div>'
    blocks = []
    for q in items:
        node = store.node_by_id(q["node_id"])
        name = node["name"] if node else q["node_id"]
        blocks.append(f"""<div class="q">
  <div><span class="code">#{_esc(q['code'])}</span>
       <span class="meta">[{_esc(name)}] {_esc(_ago(q['created_at']))}</span></div>
  <div class="prompt">{_esc((q['prompt'] or '')[:900])}</div>
  <form method="post" action="/ui/answer">
    <input type="hidden" name="code" value="{_esc(q['code'])}">
    <input type="text" name="answer" placeholder="输入答复，例如 yes / allow / 你的意见" autocomplete="off" required>
    <button type="submit">答复</button>
  </form>
</div>""")
    return "".join(blocks)


def _chat(store: Any) -> str:
    msgs = store.chat_recent(60)
    if not msgs:
        body = '<div class="none">还没有对话。问问进度，例如「现在几台机器在线？」「workbench 在干什么？」</div>'
    else:
        rows = []
        for m in msgs:
            role = m.get("role") or "bot"
            rows.append(
                f'<div class="row {role}"><div class="bubble">{_esc(m.get("text"))}</div>'
                f'<span class="time">{_esc(_clock(m.get("ts")))}</span></div>'
            )
        body = "".join(rows)
    return f"""<div class="log">{body}</div>
<form method="post" action="/ui/chat">
  <input type="hidden" name="chat" value="1">
  <input type="text" name="text" placeholder="说点什么…（含 #编号 则按决策答复处理）" autocomplete="off" required>
  <button type="submit">发送</button>
</form>"""


def render(store: Any, message: str = "", warning: str = "", selected: str = "") -> str:
    head = ""
    if message:
        head += f'<div class="ok">{_esc(message)}</div>'
    if warning:
        head += f'<div class="warn">{_esc(warning)}</div>'
    back = '<a class="all" href="/">← 全部</a>' if selected else ""
    return page(
        head
        + "<h2>项目状态</h2>" + _usage_line(store) + _lines(store, selected)
        + f"<h2>待回答的问题{back}</h2>" + _questions(store, selected)
        + "<h2>聊天</h2>" + _chat(store)
    )


def _session_value(user: str, password: str) -> str:
    return hashlib.sha256(f"{user}|{password}|arb-ui".encode()).hexdigest()


def handle_ui(
    store: Any, hub: Any, method: str, query: dict, form: dict, cookie: str = ""
) -> tuple[int, str, str]:
    user = str(getattr(hub, "ui_user", "") or DEFAULT_USER)
    password = str(getattr(hub, "ui_password", "") or DEFAULT_PASSWORD)
    if not user or not password:
        return 404, page('<div class="warn">网页控制台未启用：请配置 <code>ui_user</code> / <code>ui_password</code>。</div>'), ""

    selected = str(query.get("m") or form.get("m") or "").strip()
    session = _session_value(user, password)
    set_cookie = f"{COOKIE}={session}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800"
    authorized = bool(cookie) and hmac.compare_digest(cookie, session)

    if method == "POST" and form.get("user") is not None:
        if hmac.compare_digest(str(form.get("user") or ""), user) and hmac.compare_digest(
            str(form.get("password") or ""), password
        ):
            return 200, render(store, selected=selected), set_cookie
        return 403, login_form("账号或密码不正确。"), ""

    if not authorized:
        return 200, login_form(), ""

    if method == "POST" and form.get("chat"):
        text = str(form.get("text") or "").strip()[:MAX_ANSWER]
        if text:
            store.chat_add("me", text)
            m = CODE_RE.search(text)
            q = store.resolve(m.group(1)) if m else None
            if q is not None and q["state"] == "pending":
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
                        usage = getattr(advisor, "last_usage", {}) or {}
                        if usage.get("prompt") or usage.get("completion"):
                            store.usage_add("advisor", usage.get("prompt", 0), usage.get("completion", 0))
                    except Exception as exc:
                        reply = None
                        store.audit("web-console", "chat_error", {"error": str(exc)[:150]})
                    store.chat_add("bot", reply or "（顾问暂时没有回答）")
        return 200, render(store, selected=selected), set_cookie

    if method == "GET" or not str(form.get("code") or "").strip():
        return 200, render(store, selected=selected), set_cookie

    code = str(form.get("code") or "").strip().upper()
    answer = str(form.get("answer") or "").strip()[:MAX_ANSWER]
    if not code or not answer:
        return 400, render(store, warning="编号和答复都不能为空。", selected=selected), set_cookie

    q = store.resolve(code)
    if q is None:
        return 404, render(store, warning=f"没有 #{code} 这个待决策项。", selected=selected), set_cookie
    if q["state"] != "pending":
        return 409, render(store, warning=f"#{code} 已经处理过了（{q['state']}），未被采纳。", selected=selected), set_cookie

    owner = (hub.owner_ids or ["web-console"])[0]
    store.push(q["node_id"], answer, kind="answer", code=code, frm=owner)
    store.audit("web-console", "answer_sent", {"code": code, "answer": answer[:80]})
    node = store.node_by_id(q["node_id"])
    name = node["name"] if node else q["node_id"]
    store.chat_add("sys", f"已把答复交给 {name}（#{code}）。")
    return 200, render(
        store, message=f"已把答复交给 {name}（#{code}）。它接受后会把 #{code} 关闭。", selected=selected
    ), set_cookie

"""A small web console on the hub: see pending questions, answer them.

Safety model (important):
  * the page is served by the hub over the same TLS port, and requires a token
    from hub.json (`ui_token`); without it the UI is disabled entirely
  * answering means "push this text into the node's inbox", exactly like a
    WeChat reply - the node's own router still re-checks expiry, the dangerous
    operation gate and the once-only transition. The web page cannot approve
    anything the phone could not, and the hub still never closes a question
    itself (the node does after it accepts the answer)

Layout is deliberately plain HTML with a 5s refresh: no build step, no JS
framework, works on a phone browser.
"""

from __future__ import annotations

import hmac
import html
import time
from typing import Any

MAX_ANSWER = 2000


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


def page(body: str, token: str, title: str = "Agent Remote Bridge") -> str:
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>{_esc(title)}</title>
<style>
 :root {{ color-scheme: light dark }}
 body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 0; padding: 16px; line-height: 1.5; }}
 h1 {{ font-size: 18px; margin: 0 0 12px }}
 .q {{ border: 1px solid #8884; border-radius: 10px; padding: 12px; margin: 0 0 12px }}
 .code {{ font-family: ui-monospace, Consolas, monospace; font-weight: 700 }}
 .meta {{ opacity: .75; font-size: 13px }}
 .prompt {{ white-space: pre-wrap; margin: 8px 0; font-size: 14px }}
 form {{ display: flex; gap: 8px; margin-top: 8px }}
 input[type=text] {{ flex: 1; padding: 10px; font-size: 16px; border-radius: 8px;
                     border: 1px solid #8886 }}
 button {{ padding: 10px 16px; font-size: 16px; border-radius: 8px; cursor: pointer }}
 .ok {{ background: #1a7f3722; border-color: #1a7f37; padding: 10px; border-radius: 8px }}
 .warn {{ background: #bb800922; padding: 10px; border-radius: 8px }}
 .empty {{ opacity: .7 }}
 footer {{ margin-top: 20px; font-size: 12px; opacity: .6 }}
</style>
</head><body>
<h1>{_esc(title)}</h1>
{body}
<footer>每 5 秒自动刷新 · 答复会像微信回复一样交给对应机器，由它本地复验后执行</footer>
</body></html>
"""


def _question_block(q: dict, token: str) -> str:
    return f"""<div class="q">
  <div><span class="code">#{_esc(q.get('code'))}</span>
       <span class="meta">[{_esc(q.get('node_name'))}] {_esc(_age(q.get('created_at')))}</span></div>
  <div class="prompt">{_esc((q.get('prompt') or '')[:900])}</div>
  <form method="post" action="/ui/answer">
    <input type="hidden" name="token" value="{_esc(token)}">
    <input type="hidden" name="code" value="{_esc(q.get('code'))}">
    <input type="text" name="answer" placeholder="输入答复，例如 yes / allow / 你的意见" autocomplete="off" required>
    <button type="submit">答复</button>
  </form>
</div>"""


def render(store: Any, token: str, message: str = "", warning: str = "") -> str:
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
        body = head + "".join(_question_block(q, token) for q in rows)
    return page(body, token)


def handle_ui(store: Any, hub: Any, method: str, query: dict, form: dict) -> tuple[int, str]:
    expected = str(getattr(hub, "ui_token", "") or "")
    if not expected:
        return 404, page('<p class="warn">网页控制台未启用：请在中转站配置 <code>ui_token</code>。</p>', "")
    given = str(form.get("token") or query.get("token") or "")
    if not hmac.compare_digest(given, expected):
        return 403, page('<p class="warn">令牌不正确。请在 URL 后加 <code>?token=…</code>。</p>', "")

    if method == "GET":
        return 200, render(store, given)

    code = str(form.get("code") or "").strip().upper()
    answer = str(form.get("answer") or "").strip()[:MAX_ANSWER]
    if not code or not answer:
        return 400, render(store, given, warning="编号和答复都不能为空。")

    q = store.resolve(code)
    if q is None:
        return 404, render(store, given, warning=f"没有 #{code} 这个待决策项。")
    if q["state"] != "pending":
        return 409, render(store, given, warning=f"#{code} 已经处理过了（{q['state']}），未被采纳。")

    # Same path as a WeChat reply: hand the text to the machine, which re-checks
    # everything locally and closes the code only if it accepts it.
    owner = (hub.owner_ids or ["web-console"])[0]
    store.push(q["node_id"], answer, kind="answer", code=code, frm=owner)
    store.audit("web-console", "answer_sent", {"code": code, "answer": answer[:80]})
    node = store.node_by_id(q["node_id"])
    name = node["name"] if node else q["node_id"]
    return 200, render(
        store, given,
        message=f"已把答复交给 {name}（#{code}）。它接受后会把 #{code} 关闭；这条页面刷新即可看到结果。",
    )

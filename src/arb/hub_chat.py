"""M3: a read-only chat/advisor on the hub, backed by DeepSeek.

What it is allowed to do: read the hub's own state (which machines are online,
their projects and last snapshot, which decisions are pending) and answer a
free-text WeChat message with a short, useful reply.

What it must never do: approve anything, execute anything, or route a decision.
It is only consulted when the inbound text is *not* a decision answer -
`Hub.handle_human` checks quote/#CODE/pending before it ever gets here - and it
returns text, not a verdict. The dangerous-operation path never touches it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
SYSTEM_PROMPT = (
    "你是「Agent Remote Bridge」的中控助手。人类在微信里向你提问，"
    "你只能根据下面提供的机器状态作答，不能编造。\n"
    "硬性规则：\n"
    "1) 你没有任何审批权限。若人类让你批准/放行某个操作，回答：需要他本人在微信里对那条决策消息回复。\n"
    "2) 不要声称你执行了任何命令或修改了任何文件。\n"
    "3) 中文回答，<= 300 字，先给结论再给依据；不确定就说不知道。\n"
    "4) 不要输出 Markdown 表格或代码块，微信不渲染。"
)


class Advisor:
    def __init__(
        self,
        api_key: str,
        store: Any,
        *,
        base_url: str = DEFAULT_BASE,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        max_per_minute: int = 6,
    ):
        self.api_key = api_key
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_per_minute = max_per_minute
        self._calls: list[float] = []
        self.last_usage: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- rate limiting ---------------------------------------------------
    def _allow(self) -> bool:
        now = time.time()
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.max_per_minute:
                return False
            self._calls.append(now)
            return True

    # -- context ---------------------------------------------------------
    def context(self, node_names: list[str] | None = None) -> str:
        lines: list[str] = []
        for n in self.store.nodes():
            snap = n.get("snapshot") or ""
            try:
                snap_obj = json.loads(snap) if isinstance(snap, str) and snap else {}
            except ValueError:
                snap_obj = {}
            bits = [
                f"机器 {n['name']}",
                "在线" if n.get("online") else "离线",
            ]
            projs = n.get("projects")
            if projs and projs != "[]":
                bits.append(f"项目 {projs}")
            if snap_obj:
                summary = snap_obj.get("summary") or snap_obj.get("git") or ""
                if summary:
                    bits.append(f"快照 {str(summary)[:160]}")
            lines.append("; ".join(bits))
        pending = self.store.pending()
        if pending:
            lines.append("待决策: " + "; ".join(f"#{p['code']} {str(p['prompt'])[:40]}" for p in pending))
        else:
            lines.append("待决策: 无")
        if not lines:
            lines.append("还没有任何机器注册。")
        return "\n".join(lines)

    # -- llm call --------------------------------------------------------
    def _complete(self, messages: list[dict[str, str]]) -> str:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0.3, "max_tokens": 500}).encode()
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        usage = payload.get("usage") or {}
        try:
            self.last_usage = {
                "prompt": int(usage.get("prompt_tokens") or 0),
                "completion": int(usage.get("completion_tokens") or 0),
            }
        except (TypeError, ValueError):
            self.last_usage = {}
        return str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()

    def reply(self, text: str, node_names: list[str] | None = None) -> str | None:
        if not self._allow():
            return "（聊天层限流中：每分钟最多几次，稍后再问或直接发 /machines）"
        ctx = self.context(node_names)
        try:
            out = self._complete([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"当前机器状态：\n{ctx}\n\n人类的问题：{text}"},
            ])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if self.store is not None:
                self.store.audit("chat", "llm_http_error", {"status": exc.code, "detail": detail})
            return f"（聊天层调用失败 HTTP {exc.code}）"
        except Exception as exc:
            if self.store is not None:
                self.store.audit("chat", "llm_error", {"error": str(exc)[:150]})
            return "（聊天层暂时不可用）"
        return out or None

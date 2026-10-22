"""M4: proactive suggestions, with a gate that keeps it quiet by default.

The hard part of a 'proactive' feature is *not* speaking. This module only
speaks when it sees a concrete, changed signal, and at most a few times an hour:

  * a machine went offline or came back
  * a decision has been waiting for a long time
  * a node's snapshot shows new uncommitted work or a branch change

Every message is labelled as a suggestion and is prose - it can never approve,
execute or route anything. `evaluate()` is pure (state in, text out) so the
gate is testable without a clock or a network.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

MAX_PER_HOUR = 3
LONG_WAIT_S = 900


def _signal(state: dict[str, Any]) -> str:
    """A stable fingerprint of everything we might comment on."""
    nodes = sorted(
        (n.get("name"), bool(n.get("online")), (n.get("snapshot") or "")[:200])
        for n in state.get("nodes", [])
    )
    pending = sorted((p.get("code"), p.get("node_id")) for p in state.get("pending", []))
    return hashlib.sha1(json.dumps([nodes, pending], ensure_ascii=False).encode()).hexdigest()


def evaluate(state: dict[str, Any], last_signal: str, sent_last_hour: int) -> tuple[str | None, str]:
    """Return (message or None, new_last_signal).

    Rules, in order: stay silent if nothing changed, stay silent when over the
    hourly budget, otherwise pick the most actionable thing that is true.
    """
    sig = _signal(state)
    if sig == last_signal:
        return None, last_signal
    if sent_last_hour >= MAX_PER_HOUR:
        return None, last_signal

    now = state.get("now") or time.time()
    offline = [n["name"] for n in state.get("nodes", []) if not n.get("online")]
    long_wait = [p for p in state.get("pending", []) if now - (p.get("created_at") or now) > LONG_WAIT_S]
    dirty = [
        (n["name"], (n.get("snapshot") or ""))
        for n in state.get("nodes", [])
        if "未提交" in (n.get("snapshot") or "") and "0 个未提交" not in (n.get("snapshot") or "")
    ]

    if long_wait:
        p = long_wait[0]
        return (
            f"💡 建议：#{p.get('code')} 已经等你超过 {int(LONG_WAIT_S/60)} 分钟了。"
            f"可以引用那条消息直接回，或回复 /pending 看看全部。",
            sig,
        )
    if offline:
        return f"💡 提醒：{', '.join(offline)} 当前离线，发给它们的消息会先缓冲，等它们回来补发。", sig
    if dirty:
        name, snap = dirty[0]
        return f"💡 建议：{name} 上还有未提交改动（{snap[:80]}），收工前可以考虑提交。", sig
    return None, sig


class Proposer:
    def __init__(self, hub: Any, store: Any, *, interval: float = 60.0, advise: bool = True):
        self.hub = hub
        self.store = store
        self.interval = interval
        self.advise = advise
        self._last_signal = ""
        self._sent: list[float] = []
        self._stop = threading.Event()

    def snapshot_state(self) -> dict[str, Any]:
        nodes = []
        for n in self.store.nodes():
            nodes.append({
                "name": n["name"],
                "online": n.get("online"),
                "snapshot": ((n.get("snapshot") or "") if isinstance(n.get("snapshot"), str) else "")
                and (json.loads(n["snapshot"]).get("summary") if n.get("snapshot") else ""),
            })
        return {"nodes": nodes, "pending": self.store.pending(), "now": time.time()}

    def tick(self) -> str | None:
        now = time.time()
        sent = len([t for t in self._sent if now - t < 3600])
        text, self._last_signal = evaluate(self.snapshot_state(), self._last_signal, sent)
        if text:
            self._sent.append(now)
            self.hub.say(text)
            self.store.audit("proactive", "sent", {"text": text[:120]})
        return text

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # never let advice break the hub
                self.store.audit("proactive", "error", {"error": str(exc)[:150]})

    def stop(self) -> None:
        self._stop.set()


def start_thread(hub: Any, store: Any, cfg: dict[str, Any]) -> Proposer:
    prop = Proposer(hub, store, interval=float(cfg.get("proactive_interval_s", 60)))
    threading.Thread(target=prop.run, name="proactive", daemon=True).start()
    return prop

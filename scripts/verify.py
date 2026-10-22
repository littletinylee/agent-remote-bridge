"""One-shot health/acceptance snapshot, safe to run any time.

    python scripts/verify.py            # human readable + JSON
    python scripts/verify.py --markdown # markdown table for docs

It never mutates state: reads the config/DB, probes the WeChat endpoint, asks the
daemon for health. Exit code 0 when the mandatory checks pass.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def arb_home() -> Path:
    env = os.environ.get("ARB_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", "")) / "agent-remote-bridge"
    return Path.home() / ".config" / "agent-remote-bridge"


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, ok: bool | None, detail: str, required: bool = True) -> None:
        self.rows.append({"check": name, "ok": ok, "detail": detail, "required": required})

    def render(self) -> str:
        out = []
        for r in self.rows:
            mark = "✅" if r["ok"] else ("⏳" if r["ok"] is None else "❌")
            if not r["required"] and r["ok"] is False:
                mark = "⚠️"
            out.append(f"{mark} **{r['check']}** — {r['detail']}")
        return "\n".join(out)

    def markdown(self) -> str:
        lines = ["| 检查项 | 结果 | 详情 |", "|---|---|---|"]
        for r in self.rows:
            mark = "✅" if r["ok"] else ("⏳" if r["ok"] is None else "❌")
            if not r["required"] and r["ok"] is False:
                mark = "⚠️"
            lines.append(f"| {r['check']} | {mark} | {r['detail']} |")
        return "\n".join(lines)

    @property
    def ok(self) -> bool:
        return all(r["ok"] for r in self.rows if r["required"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--out", default="verify-report.json")
    args = ap.parse_args()

    chk = Checks()
    home = arb_home()
    chk.add("Python", sys.version_info >= (3, 9), f"{platform.python_version()} on {platform.system()}")

    cfg_path = home / "config.json"
    cfg: dict = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    chk.add("配置文件", cfg_path.exists(), str(cfg_path))

    creds_path = home / "ilink.json"
    creds = json.loads(creds_path.read_text(encoding="utf-8-sig")) if creds_path.exists() else {}
    chk.add("微信凭据", bool(creds.get("token")), f"account={creds.get('account_id', '-')}" if creds else "未登录 (arb login)")

    db_path = home / "arb.sqlite3"
    stats: dict = {}
    if db_path.exists():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            stats = {
                "sessions": con.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
                "pending": con.execute("SELECT COUNT(*) c FROM questions WHERE state='pending'").fetchone()["c"],
                "answered": con.execute("SELECT COUNT(*) c FROM questions WHERE state='answered'").fetchone()["c"],
                "expired": con.execute("SELECT COUNT(*) c FROM questions WHERE state='expired'").fetchone()["c"],
                "inbound": con.execute("SELECT COUNT(*) c FROM inbound").fetchone()["c"],
                "outbox_unsent": con.execute("SELECT COUNT(*) c FROM outbox WHERE sent_at IS NULL").fetchone()["c"],
            }
        finally:
            con.close()
    chk.add("SQLite", db_path.exists(), f"会话 {stats.get('sessions', '-')} / 提问 {stats.get('answered', 0)} 已回答 / {stats.get('pending', 0)} 待处理")

    # live channel health via the daemon
    health: dict = {}
    if cfg.get("port") and cfg.get("api_token"):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cfg['port']}/api/health",
                headers={"X-ARB-Token": cfg["api_token"]},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                health = json.loads(resp.read().decode())
        except Exception:
            health = {}
    ch = health.get("channel") or {}
    chk.add("守护进程", bool(health), f"运行中 {health.get('uptime_s')}s" if health else "未运行 (arb start)")
    chk.add(
        "微信通道健康",
        bool(ch.get("healthy")),
        f"{ch.get('name', '-')} healthy={ch.get('healthy')} last_error={ch.get('last_error') or '-'}",
    )
    chk.add("已绑定微信用户", bool(cfg.get("owner_ids")), ", ".join(cfg.get("owner_ids") or []) or "未绑定 (/pair)")

    # endpoint reachability (unauthenticated, harmless)
    try:
        from arb.channel.ilink import IlinkClient

        api = IlinkClient(cfg.get("ilink_base_url", "https://ilinkai.weixin.qq.com"))
        t0 = time.time()
        resp = api.get_qrcode(cfg.get("ilink_bot_type", "3"))
        chk.add("iLink 端点可达", int(resp.get("ret", 0) or 0) == 0, f"{int((time.time()-t0)*1000)}ms ret={resp.get('ret')}")
    except Exception as exc:
        chk.add("iLink 端点可达", False, str(exc)[:80])

    if not args.skip_tests:
        env = dict(os.environ, PYTHONPATH=str(SRC), ARB_HOME=home.as_posix())
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        )
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        chk.add("离线测试", proc.returncode == 0, " / ".join(tail))

    print(chk.markdown() if args.markdown else chk.render())
    print()
    print("结论:", "全部通过" if chk.ok else "有必需项未通过")
    report = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "home": str(home),
        "stats": stats,
        "channel": ch,
        "checks": chk.rows,
        "ok": chk.ok,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {Path(args.out).resolve()}")
    return 0 if chk.ok else 1


if __name__ == "__main__":
    sys.exit(main())

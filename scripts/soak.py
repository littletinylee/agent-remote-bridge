"""Short soak test: keep the daemon busy and watch for leaks.

Runs concurrent ask/answer cycles against a throw-away daemon (console channel)
and samples the daemon's working set / thread count. Fails if any operation
fails or if memory grows without bound.

    python scripts/soak.py --seconds 90 --workers 3
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PY = sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def api(cfg: dict, path: str, payload: dict | None = None, timeout: float = 20) -> dict:
    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ARB-Token", cfg["api_token"])
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def proc_metrics(pid: int) -> tuple[int, int]:
    """(working set bytes, thread count) via PowerShell; 0s when unavailable."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p) {{ \"$($p.WorkingSet64) $($p.Threads.Count)\" }}"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if not out:
            return 0, 0
        ws, threads = out.split()
        return int(ws), int(threads)
    except Exception:
        return 0, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="arb-soak-"))
    home = tmp / "arbhome"
    home.mkdir(parents=True)
    cfg = {
        "port": free_port(),
        "api_token": "soak-" + os.urandom(6).hex(),
        "pairing_code": "SOAK01",
        "channel": "console",
        "owner_ids": ["console"],
        "notify_min_interval_s": 0,
        "ask_timeout_s": 60,
    }
    (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ, ARB_HOME=str(home), PYTHONPATH=str(SRC))

    daemon = subprocess.Popen(
        [PY, "-m", "arb", "serve"], cwd=str(ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
    )
    assert daemon.stdin is not None
    for _ in range(150):
        try:
            api(cfg, "/api/health")
            break
        except Exception:
            time.sleep(0.2)
    print(f"[soak] daemon pid={daemon.pid} port={cfg['port']}")

    # Answer every question the console channel prints. We cannot see stdout
    # (DEVNULL), so poll the pending list instead: that is also a good exercise
    # of the read path.
    stop = threading.Event()
    stats = {"answered": 0, "errors": 0}
    lock = threading.Lock()

    def answerer() -> None:
        while not stop.is_set():
            try:
                pending = api(cfg, "/api/pending", timeout=5)["pending"]
            except Exception:
                time.sleep(0.2)
                continue
            for q in pending:
                try:
                    api(cfg, "/api/answer", {"code": q["code"], "text": "1", "by": "soak"})
                    with lock:
                        stats["answered"] += 1
                except Exception:
                    with lock:
                        stats["errors"] += 1
            time.sleep(0.05)

    def worker(idx: int) -> None:
        n = 0
        while not stop.is_set():
            try:
                sess = api(cfg, "/api/session", {"agent": f"soak-{idx}", "project": f"p{idx}", "cwd": f"D:/p{idx}", "key": f"k{idx}"})
                api(cfg, "/api/ask", {
                    "session_id": sess["session"]["id"],
                    "prompt": f"soak {idx}-{n}",
                    "options": ["one", "two"],
                    "timeout_s": 30,
                })
                n += 1
            except Exception:
                with lock:
                    stats["errors"] += 1
                time.sleep(0.2)
            time.sleep(0.15)

    threading.Thread(target=answerer, daemon=True).start()
    workers = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.workers)]
    for w in workers:
        w.start()

    ws0, th0 = proc_metrics(daemon.pid)
    samples = []
    started = time.time()
    try:
        while time.time() - started < args.seconds:
            time.sleep(10)
            ws, th = proc_metrics(daemon.pid)
            uptime = int(time.time() - started)
            samples.append({"t": uptime, "ws_mb": round(ws / 1e6, 1), "threads": th})
            print(f"[soak {uptime:3d}s] ws={ws/1e6:6.1f}MB threads={th:3d} answered={stats['answered']} errors={stats['errors']}")
    finally:
        stop.set()
        time.sleep(0.5)
        health = api(cfg, "/api/health")
        ws1, th1 = proc_metrics(daemon.pid)
        try:
            api(cfg, "/api/shutdown", {})
        except Exception:
            pass
        time.sleep(0.4)
        if daemon.poll() is None:
            daemon.terminate()

    growth = (ws1 - ws0) / ws0 * 100 if ws0 else 0.0
    ok = stats["errors"] == 0 and stats["answered"] > 0 and growth < 100
    report = {
        "ok": ok,
        "seconds": round(time.time() - started, 1),
        "workers": args.workers,
        "answered": stats["answered"],
        "errors": stats["errors"],
        "ws_start_mb": round(ws0 / 1e6, 1),
        "ws_end_mb": round(ws1 / 1e6, 1),
        "ws_growth_pct": round(growth, 1),
        "threads_start": th0,
        "threads_end": th1,
        "samples": samples,
        "stats": health.get("stats"),
        "outbox": health.get("outbox"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from report_util import write_report

    out = os.environ.get("ARB_SOAK_REPORT", "")
    if out:
        write_report(out, report)
        print(f"report: {Path(out).resolve()}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Real-agent check #3: Claude Code calling ask_human over MCP.

The hook path was verified in e2e_claude.py; this one exercises the *other*
adapter - the documented `claude mcp add arb -- python -m arb mcp` recipe -
so both integration surfaces are proven with real CLIs.

Run:  python scripts/e2e_claude_mcp.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
CODE_RE = re.compile(r"#([2-9A-HJ-NP-Z]{4})")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_util import write_report  # noqa: E402


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def api(cfg: dict, path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ARB-Token", cfg["api_token"])
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="arb-e2e-claude-mcp-"))
    arb_home = tmp / "arbhome"
    claude_home = tmp / "claudehome"
    project = tmp / "project"
    for d in (arb_home, claude_home, project):
        d.mkdir(parents=True, exist_ok=True)

    cfg = {
        "port": free_port(),
        "api_token": "e2e-" + os.urandom(6).hex(),
        "pairing_code": "E2E123",
        "channel": "console",
        "owner_ids": ["console"],
        "approval_mode": "notify",
        "notify_min_interval_s": 0,
        "ask_timeout_s": 120,
    }
    (arb_home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    # Keep the user's credentials/model env (otherwise Claude Code says
    # "Not logged in"), but start from a clean hook set so this test can only
    # succeed through MCP.
    user_settings = Path.home() / ".claude" / "settings.json"
    base = json.loads(user_settings.read_text(encoding="utf-8-sig")) if user_settings.exists() else {}
    base.pop("hooks", None)
    (claude_home / "settings.json").write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")

    mcp_config = {
        "mcpServers": {
            "arb": {
                "command": PY,
                "args": ["-m", "arb", "mcp", "--agent", "claude-code"],
                "env": {"PYTHONPATH": str(SRC), "ARB_HOME": str(arb_home)},
            }
        }
    }
    mcp_path = tmp / "mcp.json"
    mcp_path.write_text(json.dumps(mcp_config), encoding="utf-8")

    env = dict(os.environ)
    env["ARB_HOME"] = str(arb_home)
    env["PYTHONPATH"] = str(SRC)
    env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    env["MCP_TIMEOUT"] = "180000"

    daemon = subprocess.Popen(
        [PY, "-m", "arb", "serve"], cwd=str(ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert daemon.stdout is not None and daemon.stdin is not None
    transcript: list[str] = []
    answered = {"done": False}

    def pump() -> None:
        for line in daemon.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            transcript.append(line)
            print(f"[daemon] {line}")
            m = CODE_RE.search(line)
            if m and "需要你决策" in line and not answered["done"]:
                answered["done"] = True
                time.sleep(0.3)
                print("[human] -> 1")
                daemon.stdin.write("1\n")  # type: ignore[union-attr]
                daemon.stdin.flush()  # type: ignore[union-attr]

    threading.Thread(target=pump, daemon=True).start()

    result: dict = {"ok": False, "details": {}}
    try:
        for _ in range(120):
            try:
                api(cfg, "/api/health")
                break
            except Exception:
                time.sleep(0.2)
        print("[e2e] daemon up")

        prompt = (
            "Call the MCP tool named ask_human from the server 'arb' exactly once, with prompt "
            "'Which database should we use?' and options ['Postgres','SQLite']. "
            "Then tell me which option was chosen, quoting it exactly."
        )
        cmd = [
            CLAUDE, "-p", prompt,
            "--mcp-config", str(mcp_path),
            "--allowedTools", "mcp__arb__ask_human",
            "--output-format", "json",
        ]
        started = time.time()
        proc = subprocess.run(cmd, cwd=str(project), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=420)
        elapsed = time.time() - started
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        print(f"[e2e] claude exit={proc.returncode} in {elapsed:.1f}s")
        print("[claude stdout] " + (proc.stdout or "")[-2500:])
        if proc.stderr:
            print("[claude stderr] " + proc.stderr[-1000:])

        stats = api(cfg, "/api/status")["stats"]
        result["details"] = {
            "elapsed_s": round(elapsed, 1),
            "claude_exit": proc.returncode,
            "question_answered": stats["answered"],
            "used_answer": "postgres" in out.lower(),
            "asked_via_mcp": bool(answered["done"]),
            "stats": stats,
        }
        result["ok"] = bool(answered["done"] and stats["answered"] >= 1 and "postgres" in out.lower())
    except Exception as exc:
        result["details"]["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[e2e] FAIL: {exc}")
    finally:
        try:
            api(cfg, "/api/shutdown", {})
        except Exception:
            pass
        time.sleep(0.3)
        if daemon.poll() is None:
            daemon.terminate()
        write_report("e2e-claude-mcp-report.json", result)
        print("\n=== E2E CLAUDE/MCP RESULT ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

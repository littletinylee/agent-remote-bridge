"""MCP stdio smoke test - cross-platform, no shell quoting involved.

    python scripts/mcp_smoke.py

Starts `python -m arb mcp`, sends `initialize` + `tools/list`, and asserts the
handshake and the tool list come back. Exits non-zero (printing what happened)
if not. Used by CI and handy when debugging an MCP client.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("ARB_HOME", str(Path(os.environ.get("TEMP") or "/tmp") / "arb-mcp-smoke"))

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "arb", "mcp"],
        cwd=str(ROOT),
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    print(f"exit={proc.returncode} responses={len(lines)}")
    for ln in lines:
        print("  " + ln[:160])
    if proc.stderr.strip():
        print("stderr:")
        for ln in proc.stderr.strip().splitlines()[-10:]:
            print("  " + ln)

    if proc.returncode != 0:
        print("FAIL: mcp process exited non-zero")
        return 1
    if len(lines) < 2:
        print("FAIL: expected at least 2 JSON-RPC responses (initialize + tools/list)")
        return 1

    parsed = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except ValueError:
            print(f"FAIL: non-JSON line on stdout: {ln[:120]}")
            return 1

    init = next((p for p in parsed if p.get("id") == 1), None)
    tools = next((p for p in parsed if p.get("id") == 2), None)
    if not init or "result" not in init or not init["result"].get("protocolVersion"):
        print("FAIL: initialize did not return a protocolVersion")
        return 1
    names = {t["name"] for t in (tools or {}).get("result", {}).get("tools", [])}
    expected = {"ask_human", "notify", "progress", "check_answer", "list_pending"}
    if names != expected:
        print(f"FAIL: tool list mismatch: {sorted(names)} != {sorted(expected)}")
        return 1
    print(f"ok: protocolVersion={init['result']['protocolVersion']} tools={len(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

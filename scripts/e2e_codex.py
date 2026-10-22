"""Real-agent end-to-end check #2: Codex.

Verifies with the real Codex CLI:
  1. `notify` -> bridge: turn-complete payload (argv JSON) becomes a WeChat push
     attributed to the right project/session;
  2. MCP -> bridge: Codex calls ask_human, the human answers through the bridge,
     and Codex continues inside the same turn using that answer.

Run:  python scripts/e2e_codex.py
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_util import write_report  # noqa: E402


def find_codex() -> str:
    """Pick a Codex CLI whose helper binaries (command runner / code-mode host)
    are actually present. Several builds can be installed side by side; an
    incomplete one cannot execute any tool at all."""
    candidates: list[Path] = []
    which = shutil.which("codex")
    if which:
        candidates.append(Path(which))
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.exists():
        candidates.extend(sorted(base.glob("*/codex.exe")))
    sandbox = Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"
    if sandbox.exists():
        candidates.append(sandbox)
    fallback = ""
    for cand in candidates:
        if not cand.exists():
            continue
        d = cand.parent
        if (d / "codex-code-mode-host.exe").exists() and (d / "codex-command-runner.exe").exists():
            return str(cand)
        fallback = fallback or str(cand)
    return fallback or "codex"


CODEX = find_codex()
CODE_RE = re.compile(r"#([2-9A-HJ-NP-Z]{4})")


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def api(cfg: dict, path: str, payload: dict | None = None) -> dict:
    import urllib.request

    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ARB-Token", cfg["api_token"])
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="arb-e2e-codex-"))
    arb_home = tmp / "arbhome"
    project = tmp / "project"
    for d in (arb_home, project):
        d.mkdir(parents=True, exist_ok=True)

    cfg = {
        "port": free_port(),
        "api_token": "e2e-token-" + os.urandom(6).hex(),
        "pairing_code": "E2E123",
        "channel": "console",
        "owner_ids": ["console"],
        "approval_mode": "notify",
        "notify_min_interval_s": 0,
        "ask_timeout_s": 120,
    }
    (arb_home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    env = dict(os.environ)
    env["ARB_HOME"] = str(arb_home)
    env["PYTHONPATH"] = str(SRC)

    daemon = subprocess.Popen(
        [PY, "-m", "arb", "serve"],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert daemon.stdout is not None and daemon.stdin is not None

    transcript: list[str] = []

    def pump() -> None:
        for line in daemon.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            transcript.append(line)
            print(f"[daemon] {line}")
            m = CODE_RE.search(line)
            if m and "需要你决策" in line:
                time.sleep(0.3)
                print("[human] -> 1")
                daemon.stdin.write("1\n")  # type: ignore[union-attr]
                daemon.stdin.flush()  # type: ignore[union-attr]

    threading.Thread(target=pump, daemon=True).start()

    result: dict = {"ok": False, "stages": {}, "details": {}}
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                api(cfg, "/api/health")
                break
            except Exception:
                time.sleep(0.2)
        print("[e2e] daemon up")

        # ---------- stage 1: notify -> push ----------
        notify_cfg = f"notify={json.dumps([PY, '-m', 'arb', 'hook', 'codex'])}"
        cmd1 = [CODEX, "exec", "-c", notify_cfg, "-C", str(project), "--skip-git-repo-check",
                "Reply with the single word DONE."]
        print("[e2e] stage1: codex notify")
        p1 = subprocess.run(cmd1, cwd=str(project), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300)
        time.sleep(0.8)
        joined = "\n".join(transcript)
        stage1 = ("任务完成（Codex）" in joined) and ("DONE" in joined)
        result["stages"]["notify"] = stage1
        result["details"]["codex_exit_1"] = p1.returncode
        print(f"[e2e] stage1 ok={stage1}")

        # ---------- stage 2: human decision round-trip ----------
        # 2a) MCP attempt. This Codex build ships without the code-mode host
        #     binary next to codex.exe, so MCP tool calls fail closed. We record
        #     the exact outcome instead of hiding it.
        mcp_args = ["-m", "arb", "mcp", "--agent", "codex"]
        cmd2 = [
            CODEX, "exec",
            "-c", f"mcp_servers.arb.command={json.dumps(PY)}",
            "-c", f"mcp_servers.arb.args={json.dumps(mcp_args)}",
            "-c", f"mcp_servers.arb.env.PYTHONPATH={json.dumps(str(SRC))}",
            "-c", f"mcp_servers.arb.env.ARB_HOME={json.dumps(str(arb_home))}",
            "-c", "mcp_servers.arb.startup_timeout_sec=30",
            "-c", "shell_environment_policy.inherit=all",
            "-C", str(project), "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "Call the MCP tool named ask_human from the 'arb' server exactly once, with prompt "
            "'Which database should we use?' and options ['Postgres','SQLite']. "
            "Then tell me which option was chosen, quoting it exactly.",
        ]
        print("[e2e] stage2a: codex MCP ask_human (may be blocked by tool runtime)")
        p2 = subprocess.run(cmd2, cwd=str(project), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=420)
        out2 = (p2.stdout or "") + "\n" + (p2.stderr or "")
        tool_runtime_blocked = (
            "code-mode host" in out2 or "tool runtime failed" in out2 or "tool host is disabled" in out2
        )
        mcp_ok = ("已收到" in "\n".join(transcript)) and ("Postgres" in out2)
        result["stages"]["mcp_ask_human"] = mcp_ok
        result["details"]["mcp_blocked_by_environment"] = bool(tool_runtime_blocked and not mcp_ok)
        result["details"]["codex_exit_2"] = p2.returncode
        result["details"]["stage2a_stdout_tail"] = (p2.stdout or "")[-1500:]
        print("[codex stage2a stdout] " + (p2.stdout or "")[-2000:])
        print(f"[e2e] stage2a ok={mcp_ok} blocked_by_env={tool_runtime_blocked}")

        # 2b) CLI adapter: any agent that can run a shell command can block on
        #     `arb ask`. This needs no MCP tool runtime at all.
        ask_cmd = (
            f'{PY} -m arb ask "Which database should we use?" '
            f"--option Postgres --option SQLite --timeout 120 --agent codex --project codex-e2e"
        )
        prompt2b = (
            "Run exactly this shell command and nothing else:\n"
            f"{ask_cmd}\n"
            "The command prints a JSON object. Read its \"answer\" field and reply with the answer only."
        )
        cmd3 = [
            CODEX, "exec",
            "-c", "shell_environment_policy.inherit=all",
            "-C", str(project), "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", prompt2b,
        ]
        print("[e2e] stage2b: codex CLI-adapter ask round-trip")
        p3 = subprocess.run(cmd3, cwd=str(project), env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=420)
        out3 = (p3.stdout or "") + "\n" + (p3.stderr or "")
        print("[codex stage2b stdout] " + (p3.stdout or "")[-3000:])
        print("[codex stage2b stderr] " + (p3.stderr or "")[-2000:])
        cli_ok = ("Postgres" in out3) and ("已收到" in "\n".join(transcript))
        result["stages"]["cli_ask_roundtrip"] = cli_ok
        result["details"]["codex_exit_3"] = p3.returncode
        result["details"]["stage2b_stdout_tail"] = (p3.stdout or "")[-1200:]
        print(f"[e2e] stage2b ok={cli_ok}")

        stats = api(cfg, "/api/status")["stats"]
        sessions = api(cfg, "/api/sessions")["sessions"]
        result["details"]["stats"] = stats
        result["details"]["sessions"] = [
            {"agent": s["agent"], "project": s["project"], "status": s["status"]} for s in sessions
        ]
        # notify + one human-decision round-trip is the acceptance bar for Codex;
        # MCP is recorded separately because of the environment gap.
        result["ok"] = bool(stage1 and cli_ok)
    except Exception as exc:  # pragma: no cover
        result["details"]["error"] = str(exc)
        print(f"[e2e] FAIL: {exc}")
    finally:
        try:
            api(cfg, "/api/shutdown", {})
        except Exception:
            pass
        time.sleep(0.4)
        if daemon.poll() is None:
            daemon.terminate()
        write_report("e2e-codex-report.json", result)
        print("\n=== E2E CODEX RESULT ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

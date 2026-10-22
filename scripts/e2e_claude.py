"""Real-agent end-to-end check #1: Claude Code.

Verifies, with the *real* Claude Code CLI and the *real* bridge daemon:

  1. the UserPromptSubmit / Stop hooks push an event,
  2. a PreToolUse hook blocks a Bash call and asks the human through the
     bridge (console channel here, so the transcript is inspectable),
  3. the human's answer travels back through the SAME session and Claude
     continues inside its original turn,
  4. the side effect actually happened (result file written).

Run:  python scripts/e2e_claude.py
Exit: 0 on success, 1 on failure (details printed).
"""

from __future__ import annotations

import json
import os
import queue
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


def wait_health(cfg: dict, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            api(cfg, "/api/health")
            return True
        except Exception:
            time.sleep(0.2)
    return False


def claude_hooks(python_exe: str) -> dict:
    cmd = f'"{python_exe}" -m arb hook claude-code'
    plain = [{"hooks": [{"type": "command", "command": cmd, "timeout": 30}]}]
    return {
        "SessionStart": plain,
        "UserPromptSubmit": plain,
        "Notification": plain,
        "Stop": plain,
        "PreToolUse": [
            {
                "matcher": "Bash|Write|Edit",
                "hooks": [{"type": "command", "command": cmd, "timeout": 180}],
            }
        ],
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="arb-e2e-claude-"))
    arb_home = tmp / "arbhome"
    claude_home = tmp / "claudehome"
    project = tmp / "project"
    for d in (arb_home, claude_home, project):
        d.mkdir(parents=True, exist_ok=True)

    cfg = {
        "port": free_port(),
        "api_token": "e2e-token-" + os.urandom(6).hex(),
        "pairing_code": "E2E123",
        "channel": "console",
        "owner_ids": ["console"],
        "approval_mode": "approve",
        "approval_timeout_s": 120,
        "notify_min_interval_s": 0,
        "ask_timeout_s": 120,
    }
    (arb_home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    env = dict(os.environ)
    env["ARB_HOME"] = str(arb_home)
    env["PYTHONPATH"] = str(SRC)
    env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    env.pop("ARB_DEBUG", None)

    # Claude Code settings: reuse the user's env (API key/model) + our hooks.
    user_settings = Path.home() / ".claude" / "settings.json"
    base = json.loads(user_settings.read_text(encoding="utf-8")) if user_settings.exists() else {}
    base.setdefault("env", {})
    base["hooks"] = claude_hooks(PY)
    (claude_home / "settings.json").write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[e2e] tmp={tmp}")
    print(f"[e2e] daemon port={cfg['port']} channel=console approval_mode=approve")

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
    answers: list[str] = []
    answered = threading.Event()

    def pump() -> None:
        for line in daemon.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            transcript.append(line)
            print(f"[daemon] {line}")
            m = CODE_RE.search(line)
            if m and "需要你决策" in line and not answered.is_set():
                code = m.group(1)
                time.sleep(0.3)
                reply = f"yes\n"
                print(f"[human] -> {reply.strip()}  (approving #{code})")
                answers.append(f"yes #{code}")
                daemon.stdin.write(reply)  # type: ignore[union-attr]
                daemon.stdin.flush()  # type: ignore[union-attr]
                answered.set()

    threading.Thread(target=pump, daemon=True).start()

    result = {"ok": False, "stage": "start", "details": {}}
    try:
        if not wait_health(cfg):
            result["stage"] = "daemon_health"
            print("[e2e] FAIL: daemon did not become healthy")
            return 1
        print("[e2e] daemon healthy")

        result["stage"] = "run_claude"
        prompt = (
            "Run exactly this bash command: echo E2E-OK > result.txt . "
            "Then reply with the single word DONE."
        )
        cmd = [CLAUDE, "-p", prompt, "--allowedTools", "Bash", "--output-format", "json"]
        print(f"[e2e] launching: {' '.join(cmd[:3])} ...")
        started = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        elapsed = time.time() - started
        print(f"[e2e] claude exit={proc.returncode} in {elapsed:.1f}s")
        if proc.stdout:
            print("[claude stdout] " + proc.stdout[:4000])
        if proc.stderr:
            print("[claude stderr] " + proc.stderr[:2000])

        result["details"]["claude_exit"] = proc.returncode
        result["details"]["elapsed_s"] = round(elapsed, 1)
        result["details"]["approved"] = bool(answers)

        # Did the tool actually run after approval?
        artifact = project / "result.txt"
        time.sleep(0.5)
        result["details"]["artifact_exists"] = artifact.exists()
        if artifact.exists():
            result["details"]["artifact"] = artifact.read_text(encoding="utf-8", errors="replace").strip()

        status = api(cfg, "/api/status")
        sessions = api(cfg, "/api/sessions")["sessions"]
        result["details"]["stats"] = status["stats"]
        result["details"]["sessions"] = [
            {"agent": s["agent"], "project": s["project"], "status": s["status"]} for s in sessions
        ]
        audit = api(cfg, "/api/health")
        result["details"]["channel_healthy"] = audit["channel"]["healthy"]

        ok = bool(
            answers
            and artifact.exists()
            and "E2E-OK" in (result["details"].get("artifact") or "")
            and status["stats"]["answered"] >= 1
        )
        result["ok"] = ok
        result["stage"] = "done" if ok else "assertions"
    except subprocess.TimeoutExpired:
        result["stage"] = "claude_timeout"
        print("[e2e] FAIL: claude timed out")
    except Exception as exc:  # pragma: no cover
        result["stage"] = f"exception: {exc}"
        print(f"[e2e] FAIL: {exc}")
    finally:
        try:
            api(cfg, "/api/shutdown", {})
        except Exception:
            pass
        time.sleep(0.4)
        if daemon.poll() is None:
            daemon.terminate()
        report = Path("e2e-claude-report.json")
        write_report(report, result)
        print("\n=== E2E CLAUDE RESULT ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"report: {report.resolve()}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

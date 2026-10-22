"""Final acceptance: a REAL agent blocked on a REAL WeChat approval.

Unlike e2e_claude.py this does not own the daemon: it uses the live bridge in
the user's normal ARB_HOME (channel=ilink), so the question really goes to the
phone. Set approval_mode=approve first.

Run:  python scripts/e2e_wechat_agent.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PY = sys.executable
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
ARB_HOME = Path(os.environ.get("ARB_HOME") or (Path(os.environ["APPDATA"]) / "agent-remote-bridge"))
CODE_RE = re.compile(r"#([2-9A-HJ-NP-Z]{4})")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from report_util import write_report  # noqa: E402


def api(path: str, payload: dict | None = None) -> dict:
    cfg = json.loads((ARB_HOME / "config.json").read_text(encoding="utf-8-sig"))
    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-ARB-Token", cfg["api_token"])
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    cfg_path = ARB_HOME / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    cfg["approval_mode"] = "approve"
    cfg["approval_timeout_s"] = int(os.environ.get("ARB_APPROVAL_TIMEOUT", "600"))
    cfg.setdefault("approval_tools", ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"])
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[e2e] approval_mode=approve, timeout={cfg['approval_timeout_s']}s, home={ARB_HOME}")

    health = api("/api/health")
    assert health["channel"]["healthy"], f"channel not healthy: {health['channel']}"
    print(f"[e2e] channel={health['channel']['name']} healthy=True")

    work = Path(tempfile.mkdtemp(prefix="wechat-e2e-"))
    claude_home = work / "claudehome"
    project = work / "project"
    claude_home.mkdir(parents=True)
    project.mkdir(parents=True)

    user_settings = Path.home() / ".claude" / "settings.json"
    base = json.loads(user_settings.read_text(encoding="utf-8-sig")) if user_settings.exists() else {}
    base.setdefault("env", {})
    cmd = f'"{PY}" -m arb hook claude-code'
    plain = [{"hooks": [{"type": "command", "command": cmd, "timeout": 60}]}]
    base["hooks"] = {
        "UserPromptSubmit": plain,
        "Stop": plain,
        "PreToolUse": [
            {
                "matcher": "Bash|Write|Edit",
                "hooks": [{"type": "command", "command": cmd, "timeout": int(cfg["approval_timeout_s"]) + 60}],
            }
        ],
    }
    (claude_home / "settings.json").write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")

    env = dict(os.environ)
    env["ARB_HOME"] = str(ARB_HOME)
    env["PYTHONPATH"] = str(SRC)
    env["CLAUDE_CONFIG_DIR"] = str(claude_home)

    prompt = (
        "Run exactly this bash command: echo WECHAT-E2E-OK > wechat_result.txt . "
        "Then reply with the single word DONE."
    )
    started = time.time()
    proc = subprocess.run(
        [CLAUDE, "-p", prompt, "--allowedTools", "Bash", "--output-format", "json"],
        cwd=str(project), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=int(cfg["approval_timeout_s"]) + 180,
    )
    elapsed = time.time() - started
    artifact = project / "wechat_result.txt"
    result = {
        "ok": False,
        "elapsed_s": round(elapsed, 1),
        "claude_exit": proc.returncode,
        "artifact_exists": artifact.exists(),
        "artifact": artifact.read_text(encoding="utf-8", errors="replace").strip() if artifact.exists() else None,
        "stdout_tail": (proc.stdout or "")[-600:],
        "stderr_tail": (proc.stderr or "")[-400:],
    }
    result["ok"] = bool(result["artifact_exists"] and "WECHAT-E2E-OK" in (result["artifact"] or ""))
    questions = api("/api/pending")
    result["still_pending"] = len(questions["pending"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    write_report("e2e-wechat-report.json", result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

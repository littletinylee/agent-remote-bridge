"""Diagnostic: invoke the Codex hook wrapper exactly like Codex does (argv)."""

import json
import os
import subprocess
from pathlib import Path

HOME = Path(os.environ["APPDATA"]) / "agent-remote-bridge"
WRAPPER = HOME / "arb-hook-codex.cmd"

CASES = {
    "SessionStart": {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "D:/tmp/x"},
    "UserPromptSubmit": {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "D:/tmp/x", "prompt": "hi"},
    "Stop": {"hook_event_name": "Stop", "session_id": "s1", "cwd": "D:/tmp/x"},
    "PreToolUse": {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "D:/tmp/x",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
    },
}

for name, payload in CASES.items():
    arg = json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run([str(WRAPPER), arg], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    print(f"=== {name}: rc={proc.returncode}")
    print("  stdout:", (proc.stdout or "").strip()[:300])
    print("  stderr:", (proc.stderr or "").strip()[-500:])

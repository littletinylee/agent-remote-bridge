"""Install the probe as the Codex plugin's hook, run codex, dump what arrived, restore."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ["APPDATA"]) / "agent-remote-bridge"
PLUGIN = HOME / "codex-marketplace" / "plugins" / "agent-remote-bridge"
PLUGIN_JSON = PLUGIN / ".codex-plugin" / "plugin.json"
PROBE = Path(__file__).resolve().parent / "_codex_hook_probe.py"
CODEX = r"C:\Users\67408\AppData\Local\OpenAI\Codex\bin\9614f1d9b40d9c4f\codex.exe"
LOG = HOME / "hook-probe.log"

backup = PLUGIN_JSON.with_suffix(".json.bak")
shutil.copy2(PLUGIN_JSON, backup)
data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
cmd = f'"{sys.executable}" "{PROBE}"'
for event, blocks in data["hooks"]["hooks"].items():
    for block in blocks:
        for hook in block["hooks"]:
            hook["command"] = cmd
PLUGIN_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

try:
    for args in (
        ["plugin", "remove", "agent-remote-bridge@agent-remote-bridge"],
        ["plugin", "add", "agent-remote-bridge@agent-remote-bridge"],
    ):
        p = subprocess.run([CODEX, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(" ".join(args), "->", p.returncode, (p.stdout or p.stderr or "").strip()[:150])

    LOG.unlink(missing_ok=True)
    work = Path(os.environ.get("TEMP", ".")) / "arb-plugin-check"
    work.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        [CODEX, "exec", "--dangerously-bypass-hook-trust", "--skip-git-repo-check", "-C", str(work),
         "Reply with the single word PLUGIN-OK."],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    for line in (p.stdout or "").splitlines() + (p.stderr or "").splitlines():
        if "hook:" in line:
            print("  ", line.strip())
    print("--- probe log ---")
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            print(line[:400])
    else:
        print("(probe never ran)")
finally:
    shutil.copy2(backup, PLUGIN_JSON)
    backup.unlink(missing_ok=True)
    for args in (
        ["plugin", "remove", "agent-remote-bridge@agent-remote-bridge"],
        ["plugin", "add", "agent-remote-bridge@agent-remote-bridge"],
    ):
        subprocess.run([CODEX, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("restored real plugin")

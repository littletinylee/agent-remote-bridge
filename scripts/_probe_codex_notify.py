"""Probe: does `codex exec` invoke the configured `notify` program?"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CODEX = shutil.which("codex") or str(Path.home() / ".codex" / ".sandbox-bin" / "codex.exe")
PROBE = Path(__file__).with_name("_probe_notify.py")
PY = sys.executable


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="codex-probe-"))
    log = work / "notify.log"
    notify = json.dumps([PY, str(PROBE), str(log)])
    cmd = [
        CODEX,
        "exec",
        "-c",
        f"notify={notify}",
        "-C",
        str(work),
        "--skip-git-repo-check",
        "Reply with the single word DONE.",
    ]
    print("RUN:", " ".join(cmd[:6]), "...")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    print("exit:", proc.returncode)
    print("stdout:", (proc.stdout or "")[-1500:])
    print("stderr:", (proc.stderr or "")[-1500:])
    print("notify fired:", log.exists())
    if log.exists():
        print("payload:", log.read_text(encoding="utf-8")[:2000])
    return 0 if log.exists() else 1


if __name__ == "__main__":
    sys.exit(main())

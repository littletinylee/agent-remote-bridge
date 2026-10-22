"""Probe: record exactly how Codex invokes a plugin hook (argv vs stdin)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

home = Path(os.environ.get("APPDATA", ".")) / "agent-remote-bridge"
log = home / "hook-probe.log"
record = {
    "t": time.strftime("%H:%M:%S"),
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env_has_pythonpath": "PYTHONPATH" in os.environ,
}
try:
    if not sys.stdin.isatty():
        record["stdin"] = sys.stdin.read()[:2000]
    else:
        record["stdin"] = "<tty>"
except Exception as exc:  # pragma: no cover
    record["stdin"] = f"<err {exc}>"
home.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
print("")

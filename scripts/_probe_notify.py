"""Tiny probe used while validating Codex's `notify` contract (dev tool)."""

import json
import sys
from pathlib import Path

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("notify-probe.log")
payload = {"argv": sys.argv, "stdin": ""}
try:
    payload["stdin"] = sys.stdin.read()
except Exception as exc:  # pragma: no cover
    payload["stdin"] = f"<error: {exc}>"
with out.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

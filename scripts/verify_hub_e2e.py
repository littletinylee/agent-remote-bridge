"""One-command end-to-end verification of the hub approval loop.

Run it, answer the phone, and it prints PASS/FAIL with the three independent
pieces of evidence:

    python scripts/verify_hub_e2e.py

PASS verifies that the command ran (the artifact file exists) and the local
question moved to answered. Hub health is diagnostic only. A #CODE reply can
also pass this test; verify answer_forwarded/source=quote in the hub audit
separately before claiming that quote routing passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAUDE = Path.home() / ".local/bin/claude.exe"


def db_path() -> Path:
    home = Path(os.environ.get("ARB_HOME") or (Path(os.environ.get("APPDATA", "")) / "agent-remote-bridge"))
    return home / "arb.sqlite3"


def pending(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("SELECT code, state FROM questions WHERE state='pending' ORDER BY created_at")]


def state_of(db: Path, code: str) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    row = con.execute("SELECT state FROM questions WHERE code=?", (code,)).fetchone()
    return row[0] if row else "missing"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", default=str(DEFAULT_CLAUDE))
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--marker", default="hub-verified.txt")
    args = ap.parse_args()

    db = db_path()
    if not db.exists():
        print(f"FAIL: no database at {db} (run `arb init` and `arb start` first)")
        return 2

    left = pending(db)
    if left:
        print("there are already pending questions; clear them first so the test is unambiguous:")
        for q in left:
            print(f"  #{q['code']}")
        print("  python scripts/clean_pending.py --db <node db>")
        return 2

    work = Path(tempfile.mkdtemp(prefix="arb-verify-"))
    marker = work / args.marker
    print(f"work dir: {work}")
    print("running Claude Code with a command that must be approved remotely...")
    cmd = [
        args.claude, "-p",
        f"Run exactly this bash command: touch {args.marker} . Then reply with the single word DONE.",
        "--allowedTools", "Bash", "--output-format", "json",
    ]
    proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    code = ""
    deadline = time.time() + 60
    while time.time() < deadline and not code:
        rows = pending(db)
        if rows:
            code = rows[0]["code"]
        time.sleep(1)
    if not code:
        proc.kill()
        print("FAIL: no question appeared (is approval_mode=approve and the daemon running?)")
        return 1

    print()
    print(f"  微信里应该收到一条 🚨 #CODE = {code}")
    print(f"  请【引用那条消息】回 yes   —— 最多等 {int(args.timeout)} 秒")
    print()

    deadline = time.time() + args.timeout
    final = "pending"
    while time.time() < deadline:
        final = state_of(db, code)
        if final != "pending":
            break
        time.sleep(2)

    try:
        _out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        err = ""

    print("evidence:")
    print(f"  1. local question #{code} state = {final}")
    print(f"  2. artifact {marker} exists = {marker.exists()}")
    hub_state = "unknown"
    hub = os.environ.get("ARB_HUB_URL", "")
    if hub:
        import ssl
        import urllib.request

        try:
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(hub.rstrip("/") + "/v1/health", timeout=10, context=ctx) as r:
                hub_state = json.loads(r.read().decode()).get("pending")
        except Exception as exc:
            hub_state = f"unreachable ({exc})"
    print(f"  3. hub pending = {hub_state}")

    ok = final == "answered" and marker.exists()
    print()
    print("PASS" if ok else "FAIL")
    if ok:
        print("Approval loop passed. Quote routing and hub code closure are not asserted by this script.")
    if not ok and err:
        print("claude stderr tail:", err[-400:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

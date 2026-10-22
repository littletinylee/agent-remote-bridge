"""Cancel stale pending questions (ops utility).

A pending code that nobody will answer is harmful: it keeps a bare reply
ambiguous and can route a late answer to a machine that moved on. The hub now
expires them on its own after `question_ttl_s`, but a local node may still hold
codes from a crashed run, so this exists for both sides.

    # on a dev machine
    python scripts/clean_pending.py --db %APPDATA%\\agent-remote-bridge\\arb.sqlite3
    # on the hub
    python scripts/clean_pending.py --db /var/lib/arb/hub.sqlite3 --hub
    # see what would go
    python scripts/clean_pending.py --db ... --dry-run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time


def clean(path: str, hub: bool, dry_run: bool, max_age: float = 0.0) -> int:
    con = sqlite3.connect(path, timeout=20)
    con.row_factory = sqlite3.Row
    q = "SELECT code, created_at, prompt FROM questions WHERE state='pending'"
    args: tuple = ()
    if max_age > 0:
        q += " AND created_at < ?"
        args = (time.time() - max_age,)
    rows = con.execute(q, args).fetchall()
    for r in rows:
        age = int((time.time() - (r["created_at"] or 0)) / 60)
        print(f"  {'close' if hub else 'cancel'} #{r['code']}  {age}分钟前  {(r['prompt'] or '')[:50]!r}")
    if dry_run:
        print(f"dry-run: {len(rows)} would be cleaned")
        return 0
    new_state = "closed" if hub else "cancelled"
    cur = con.execute(
        f"UPDATE questions SET state='{new_state}' WHERE state='pending'"
        + (" AND created_at < ?" if max_age > 0 else ""),
        args,
    )
    con.commit()
    left = con.execute("SELECT count(1) FROM questions WHERE state='pending'").fetchone()[0]
    print(f"cleaned {cur.rowcount}, pending now {left}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--hub", action="store_true", help="hub database: mark closed instead of cancelled")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--older-than", type=float, default=0.0, help="only clean questions older than N seconds")
    args = ap.parse_args()
    return clean(args.db, args.hub, args.dry_run, args.older_than)


if __name__ == "__main__":
    sys.exit(main())

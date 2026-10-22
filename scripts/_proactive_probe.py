"""Show proactive audit rows on the hub."""

import sqlite3

con = sqlite3.connect("file:/var/lib/arb/hub.sqlite3?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT ts, action, detail FROM audit WHERE actor='proactive' ORDER BY id DESC LIMIT 6"
).fetchall()
print(f"proactive rows: {len(rows)}")
for r in rows:
    print(" ", r["action"], "|", (r["detail"] or "")[:140])
nodes = con.execute("SELECT name, last_seen, status FROM nodes ORDER BY name").fetchall()
print("nodes:", [(n["name"], int(n["last_seen"] or 0)) for n in nodes])

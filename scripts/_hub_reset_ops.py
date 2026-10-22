"""One-off: drop the ops-console node so the ask helper can re-register."""

import sqlite3

con = sqlite3.connect("/var/lib/arb/hub.sqlite3")
cur = con.execute("DELETE FROM nodes WHERE name = ?", ("ops-console",))
con.commit()
print("deleted nodes:", cur.rowcount)
print("remaining:", list(con.execute("SELECT name FROM nodes")))

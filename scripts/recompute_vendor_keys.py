"""Re-apply vendor_key() to stored results after changing the grouping rules (free: no model calls).
Then re-run: python -m ap_agent.chasers

Run: python scripts/recompute_vendor_keys.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap_agent.db import connect  # noqa: E402
from ap_agent.rules import vendor_key  # noqa: E402

con = connect()
rows = con.execute("SELECT r.message_id, r.vendor_name, e.from_domain FROM email_results r JOIN emails e USING(message_id)").fetchall()
con.executemany("UPDATE email_results SET vendor_key=? WHERE message_id=?", [(vendor_key(r[1], r[2]), r[0]) for r in rows])
rows = con.execute("SELECT sha256, vendor_name FROM invoices").fetchall()
con.executemany("UPDATE invoices SET vendor_key=? WHERE sha256=?", [(vendor_key(r[1]), r[0]) for r in rows])
con.commit()
print("vendor keys recomputed for", len(rows), "invoices")

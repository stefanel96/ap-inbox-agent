"""Step 4: vendors who chase payment, how far they went, and whether the chased invoices are in the inbox.

All of this is plain code over the classified data, so it is exact and repeatable:
- a "chase" is an inbound email the model marked reminder / escalation / final_notice;
- the vendor's level is the highest level reached, with the full timeline kept;
- every invoice number a chase mentions (in the email or in a statement attached to it) is looked up
  among the invoices actually received: `found` (same number), `likely` (same number once prefixes
  and leading zeros are ignored), or `not_found`.

Run: python -m ap_agent.chasers
"""
import json
from collections import Counter, defaultdict

from .db import connect, log_issue
from .rules import loose_invoice, norm_invoice

STEP = "chasers"
LEVELS = {"none": 0, "reminder": 1, "escalation": 2, "final_notice": 3}


def _index_invoices(con):
    """Every place an invoice is present in the inbox: as a document, or as an invoice email."""
    by_norm, by_loose = defaultdict(list), defaultdict(list)
    for r in con.execute("SELECT sha256, invoice_number, invoice_norm, invoice_loose, vendor_key, first_seen_message "
                         "FROM invoices WHERE invoice_norm != ''"):
        rec = dict(kind="document", ref=r["sha256"], number=r["invoice_number"], vendor_key=r["vendor_key"],
                   message=r["first_seen_message"])
        by_norm[r["invoice_norm"]].append(rec)
        by_loose[r["invoice_loose"]].append(rec)
    for r in con.execute("SELECT message_id, vendor_key, referenced_invoices FROM email_results WHERE email_type='invoice'"):
        for num in json.loads(r["referenced_invoices"] or "[]"):
            rec = dict(kind="invoice_email", ref=r["message_id"], number=num, vendor_key=r["vendor_key"],
                       message=r["message_id"])
            by_norm[norm_invoice(num)].append(rec)
            by_loose[loose_invoice(num)].append(rec)
    return by_norm, by_loose


def _pick(cands, vendor_key):
    """Prefer a candidate from the same vendor; a number alone can collide across vendors."""
    same = [c for c in cands if c["vendor_key"] == vendor_key]
    return (same or cands)[0], bool(same)


def main():
    con = connect()
    con.execute("DELETE FROM issues WHERE step=?", (STEP,))
    con.execute("DELETE FROM chase_invoice_matches")
    by_norm, by_loose = _index_invoices(con)

    chases = con.execute(
        "SELECT r.*, e.sent_at, e.subject FROM email_results r JOIN emails e USING(message_id) "
        "WHERE r.chase_level != 'none' AND e.direction='inbound' ORDER BY e.sent_at").fetchall()
    # invoice numbers listed in statements / reminders attached to chase emails count as chased too
    attached_refs = defaultdict(list)
    for r in con.execute("SELECT a.message_id, d.referenced_invoices, d.doc_type, i.invoice_number "
                         "FROM attachments a JOIN doc_results d USING(sha256) LEFT JOIN invoices i USING(sha256)"):
        attached_refs[r["message_id"]].extend(json.loads(r["referenced_invoices"] or "[]"))
        if r["invoice_number"]:
            attached_refs[r["message_id"]].append(r["invoice_number"])

    stats = Counter()
    reported = set()  # one issue per vendor + invoice number, not one per email that mentions it
    for c in chases:
        refs = json.loads(c["referenced_invoices"] or "[]") + attached_refs.get(c["message_id"], [])
        seen = set()
        for num in refs:
            key = norm_invoice(num)
            if not key or key in seen:
                continue
            seen.add(key)
            status, match, note = "not_found", None, None
            if key in by_norm:
                match, same_vendor = _pick(by_norm[key], c["vendor_key"])
                status = "found"
                note = None if same_vendor else "same number, but filed under a different vendor name"
            elif loose_invoice(num) in by_loose:
                match, same_vendor = _pick(by_loose[loose_invoice(num)], c["vendor_key"])
                status = "likely"
                note = f"matches {match['number']} once prefixes/leading zeros are ignored"
            stats[status] += 1
            con.execute(
                "INSERT INTO chase_invoice_matches VALUES (?,?,?,?,?,?,?,?)",
                (c["vendor_key"], c["message_id"], num, status, match["ref"] if match else None,
                 match["number"] if match else None, match["message"] if match else None, note))
            if status == "not_found" and (c["vendor_key"], key) not in reported:
                reported.add((c["vendor_key"], key))
                log_issue(con, STEP, "chase", c["message_id"], "chased_invoice_not_in_inbox",
                          f"{c['vendor_name']} chases invoice {num}; no invoice with that number is in this inbox "
                          f"extract (it may have arrived before 24 Aug)")
        if not refs:
            stats["chase_without_invoice_numbers"] += 1

    con.execute("DROP VIEW IF EXISTS vendor_chasers")
    con.execute("""
        CREATE VIEW vendor_chasers AS
        SELECT r.vendor_key,
               max(r.vendor_name) AS vendor_name,
               count(*) AS chase_emails,
               max(CASE r.chase_level WHEN 'reminder' THEN 1 WHEN 'escalation' THEN 2 WHEN 'final_notice' THEN 3 END) AS max_level_n,
               CASE max(CASE r.chase_level WHEN 'reminder' THEN 1 WHEN 'escalation' THEN 2 WHEN 'final_notice' THEN 3 END)
                    WHEN 1 THEN 'reminder' WHEN 2 THEN 'escalation' WHEN 3 THEN 'final_notice' END AS max_level,
               min(e.sent_at) AS first_chase, max(e.sent_at) AS last_chase,
               sum(r.unsure) AS unsure_emails
        FROM email_results r JOIN emails e USING(message_id)
        WHERE r.chase_level != 'none' AND e.direction = 'inbound'
        GROUP BY r.vendor_key""")
    con.commit()
    n_vendors = con.execute("SELECT count(*) FROM vendor_chasers").fetchone()[0]
    levels = dict(con.execute("SELECT max_level, count(*) FROM vendor_chasers GROUP BY max_level").fetchall())
    print(f"chase emails: {len(chases)}  vendors chasing: {n_vendors}  by highest level: {levels}")
    print(f"chased invoice numbers: {dict(stats)}")


if __name__ == "__main__":
    main()

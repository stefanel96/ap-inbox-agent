"""The agent's tools: read-only questions over the processed inbox.

Every tool opens the database read-only, returns plain JSON, cites message ids / file hashes so
answers can be checked, and carries its own uncertainty (confidence, `unsure`, missing files).
The same functions are served to Hermes over MCP (mcp_server.py) and to the fallback chat (chat_cli.py).
"""
import json
import sqlite3
from collections import Counter

from .db import connect_readonly

LEVEL_ORDER = {"reminder": 1, "escalation": 2, "final_notice": 3}


def _q(sql, args=()):
    con = connect_readonly()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _one(sql, args=()):
    rows = _q(sql, args)
    return rows[0] if rows else None


def _spend() -> dict:
    """Every dollar spent on model calls, split by what it bought; the parts add up to the total.
    A call is 'in use' when it is the latest successful call for its item (that answer is what the tools show)."""
    rows = _q("WITH last AS (SELECT max(id) AS id FROM llm_calls WHERE ok=1 GROUP BY step, ref) "
              "SELECT step, id IN (SELECT id FROM last) AS in_use, count(*) AS calls, sum(ok=0) AS failed, "
              "coalesce(sum(cost_usd), 0) AS usd FROM llm_calls GROUP BY 1, 2")

    def part(pred):
        sel = [r for r in rows if pred(r)]
        return dict(usd=round(sum(r["usd"] for r in sel), 2), calls=sum(r["calls"] for r in sel))
    run = dict(documents=part(lambda r: r["in_use"] and r["step"] == "classify_doc"),
               emails=part(lambda r: r["in_use"] and r["step"] == "classify_email"))
    run["total_usd"] = round(run["documents"]["usd"] + run["emails"]["usd"], 2)
    failed = _one("SELECT count(*) AS calls, round(coalesce(sum(cost_usd), 0), 3) AS usd FROM llm_calls WHERE ok=0")
    return dict(
        one_full_run=run,
        replaced_answers=part(lambda r: not r["in_use"] and r["step"].startswith("classify")),
        accuracy_check=part(lambda r: r["step"].startswith("review")),
        failed_attempts=failed,
        total_usd=round(sum(r["usd"] for r in rows), 2))


def _needs_attention() -> list:
    """Business-facing problems, in plain words (technical details are in cost_report / list_problems)."""
    c = {r["issue_type"]: r["n"] for r in _q("SELECT issue_type, count(*) AS n FROM issues GROUP BY issue_type")}
    unsure = {r["scope"]: r["n"] for r in _q("SELECT scope, count(*) AS n FROM issues "
                                              "WHERE issue_type='unsure_classification' GROUP BY scope")}
    return [
        dict(item="attachments listed on emails but not delivered", n=c.get("file_missing", 0)),
        dict(item="files that could not be opened (password-protected or unsupported type)",
             n=c.get("password_protected", 0) + c.get("unsupported", 0) + c.get("unreadable", 0)),
        dict(item="unsure classifications (low confidence or rules disagree)", n=sum(unsure.values()),
             emails=unsure.get("email", 0), documents=unsure.get("file", 0)),
        dict(item="invoices failing a check (math, dates, missing number or total)", n=c.get("invoice_check_failed", 0)),
        dict(item="invoice numbers vendors chase that are not in this inbox (it starts 24 Aug)",
             n=c.get("chased_invoice_not_in_inbox", 0)),
    ]


def inbox_overview() -> dict:
    """Big picture: what the inbox contains, how much was read, what needs attention, what one run costs."""
    return dict(
        period=_one("SELECT min(sent_at) AS first, max(sent_at) AS last, count(*) AS emails FROM emails"),
        emails_by_direction=_q("SELECT direction, count(*) AS n FROM emails GROUP BY direction"),
        emails_by_type=_q("SELECT email_type, count(*) AS n, sum(unsure) AS unsure FROM email_results "
                          "GROUP BY email_type ORDER BY n DESC"),
        payment_chase_emails=_q("SELECT chase_level, count(*) AS n FROM email_results r JOIN emails e USING(message_id) "
                                "WHERE chase_level != 'none' AND e.direction='inbound' GROUP BY chase_level"),
        attachments=_q("SELECT status, count(*) AS n FROM attachments GROUP BY status ORDER BY n DESC"),
        unique_files_by_how_read=_q("SELECT status, read_method, count(*) AS n FROM files GROUP BY 1,2 ORDER BY n DESC"),
        documents_by_type=_q("SELECT doc_type, count(*) AS unique_files, sum(unsure) AS unsure FROM doc_results "
                             "GROUP BY doc_type ORDER BY unique_files DESC"),
        invoices=_one(
            "SELECT count(*) AS invoice_documents, "
            "count(DISTINCT CASE WHEN invoice_norm != '' THEN vendor_key || '|' || invoice_norm END) AS distinct_invoices, "
            "sum(invoice_norm = '' OR invoice_norm IS NULL) AS documents_without_invoice_number, "
            "(SELECT round(sum(t), 2) FROM (SELECT max(coalesce(total, amount_due)) AS t FROM invoices "
            " WHERE invoice_norm != '' GROUP BY vendor_key, invoice_norm)) AS total_of_distinct_invoices, "
            "sum(checks_passed=0) AS failed_checks FROM invoices"),
        invoices_note="The same invoice often arrives several times (re-sent, different file), so money totals count "
                      "each vendor + invoice number once. Documents without an invoice number are not in the total. "
                      "Amounts are USD: every invoice that states a currency says USD; the rest state none.",
        possible_duplicates_across_vendor_names=_one(
            "WITH d AS (SELECT vendor_key, invoice_norm, max(coalesce(total, amount_due)) AS t FROM invoices "
            "WHERE invoice_norm != '' GROUP BY 1, 2), g AS (SELECT t, count(*) AS c FROM d GROUP BY invoice_norm, t "
            "HAVING count(*) > 1) SELECT sum(c - 1) AS extra_copies, round(sum((c - 1) * t), 2) AS usd FROM g"),
        possible_duplicates_note="The same invoice number and amount under two vendor names (a brand and its "
                                 "distributor, or a parent and a subsidiary) is probably one invoice counted twice; "
                                 "the distinct-invoice total includes these copies.",
        needs_attention=_needs_attention(),
        processing=dict(
            **_one("SELECT (SELECT count(*) FROM emails WHERE message_id NOT IN (SELECT message_id FROM email_results)) "
                   "AS emails_not_classified, (SELECT count(*) FROM files WHERE sha256 NOT IN "
                   "(SELECT sha256 FROM doc_results)) AS files_not_classified, (SELECT count(*) FROM files "
                   "WHERE read_method='vision') AS files_read_as_images"),
            one_full_run_cost_usd=_spend()["one_full_run"]["total_usd"]),
        note="Counts of documents are unique files: the same PDF attached to several emails is counted once.",
    )


def search_emails(text: str = None, email_type: str = None, vendor: str = None, chase_level: str = None,
                  date_from: str = None, date_to: str = None, only_unsure: bool = False, limit: int = 20) -> dict:
    """Find emails. text matches subject, sender or body. email_type: invoice|statement|payment_reminder|
    remittance|other. chase_level: reminder|escalation|final_notice|any. Dates as YYYY-MM-DD."""
    where, args = ["1=1"], []
    if text:
        where.append("(e.subject LIKE ? OR e.from_email LIKE ? OR e.from_name LIKE ? OR e.body LIKE ?)")
        args += [f"%{text}%"] * 4
    if email_type:
        where.append("r.email_type = ?")
        args.append(email_type)
    if vendor:
        where.append("(r.vendor_name LIKE ? OR r.vendor_key LIKE ? OR e.from_email LIKE ?)")
        args += [f"%{vendor}%"] * 3
    if chase_level:
        if chase_level == "any":
            where.append("r.chase_level != 'none'")
        else:
            where.append("r.chase_level = ?")
            args.append(chase_level)
    if date_from:
        where.append("e.sent_at >= ?")
        args.append(date_from)
    if date_to:
        where.append("e.sent_at < date(?, '+1 day')")
        args.append(date_to)
    if only_unsure:
        where.append("r.unsure = 1")
    sql = (f"SELECT e.message_id, e.sent_at, e.direction, e.from_email, e.subject, r.email_type, r.chase_level, "
           f"r.vendor_name, round(r.final_confidence,2) AS confidence, r.unsure, e.n_attachments "
           f"FROM emails e LEFT JOIN email_results r USING(message_id) WHERE {' AND '.join(where)} "
           f"ORDER BY e.sent_at DESC")
    rows = _q(sql, args)
    return dict(total_matches=len(rows), shown=min(len(rows), limit), emails=rows[:max(1, min(limit, 100))])


def get_email(message_id: str) -> dict:
    """One email in full: body, classification with reason and confidence, and every attachment with how it was read."""
    e = _one("SELECT * FROM emails WHERE message_id=?", (message_id,))
    if not e:
        return dict(error=f"no email with id {message_id}")
    e["body"] = (e["body"] or "")[:4000]
    r = _one("SELECT email_type, chase_level, vendor_name, referenced_invoices, amount_mentioned, "
             "round(final_confidence,2) AS confidence, unsure, reason, rule_type, rule_chase FROM email_results "
             "WHERE message_id=?", (message_id,))
    atts = _q("SELECT a.attachment_id, a.filename, a.status, a.detail, f.kind, f.pages, f.read_method, "
              "f.status AS read_status, d.doc_type, d.other_kind, round(d.final_confidence,2) AS confidence, "
              "d.unsure, d.reason, i.invoice_number, i.total, i.due_date, i.check_notes, a.sha256 "
              "FROM attachments a LEFT JOIN files f USING(sha256) LEFT JOIN doc_results d USING(sha256) "
              "LEFT JOIN invoices i USING(sha256) WHERE a.message_id=? AND a.status != 'missing_inline_image'",
              (message_id,))
    hidden = _one("SELECT count(*) AS n FROM attachments WHERE message_id=? AND status='missing_inline_image'",
                  (message_id,))["n"]
    issues = _q("SELECT issue_type, detail FROM issues WHERE ref=? OR ref LIKE ?", (message_id, f"{message_id}/%"))
    # Audit-only notes (e.g. which model answered) stay in list_problems; they are not problems with the email.
    return dict(email=e, classification=r, attachments=atts, inline_images_not_delivered=hidden,
                issues=[i for i in issues if i["issue_type"] not in AUDIT_ONLY])


def search_invoices(vendor: str = None, invoice_number: str = None, date_from: str = None, date_to: str = None,
                    min_total: float = None, max_total: float = None, only_failed_checks: bool = False,
                    limit: int = 25) -> dict:
    """Invoices received (one per unique invoice document) with their key fields and where they arrived."""
    where, args = ["1=1"], []
    if vendor:
        where.append("(vendor_name LIKE ? OR vendor_key LIKE ?)")
        args += [f"%{vendor}%"] * 2
    if invoice_number:
        where.append("(invoice_number LIKE ? OR invoice_norm LIKE ?)")
        args += [f"%{invoice_number}%", f"%{''.join(ch for ch in invoice_number.upper() if ch.isalnum())}%"]
    if date_from:
        where.append("invoice_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("invoice_date <= ?")
        args.append(date_to)
    if min_total is not None:
        where.append("coalesce(total, amount_due) >= ?")
        args.append(min_total)
    if max_total is not None:
        where.append("coalesce(total, amount_due) <= ?")
        args.append(max_total)
    if only_failed_checks:
        where.append("checks_passed = 0")
    rows = _q(f"SELECT vendor_name, invoice_number, invoice_date, due_date, po_number, currency, subtotal, tax, total, "
              f"amount_due, checks_passed, check_notes, first_seen_message, first_seen_at, sha256, vendor_key, "
              f"invoice_norm FROM invoices WHERE {' AND '.join(where)} ORDER BY invoice_date DESC", args)
    # The same invoice often arrives several times as different files: count each vendor + number once.
    distinct = {}
    for r in rows:
        if r["invoice_norm"]:
            amount = r["total"] if r["total"] is not None else r["amount_due"]
            key = (r["vendor_key"], r["invoice_norm"])
            distinct[key] = max(distinct.get(key) or 0, amount or 0)
    no_number = sum(1 for r in rows if not r["invoice_norm"])
    for r in rows:
        r.pop("vendor_key")
        r.pop("invoice_norm")
    return dict(invoice_documents=len(rows), distinct_invoices=len(distinct),
                total_of_distinct_invoices=round(sum(distinct.values()), 2),
                documents_without_invoice_number=no_number, shown=min(len(rows), limit),
                invoices=rows[:max(1, min(limit, 100))],
                note="Totals count each vendor + invoice number once (re-sent copies are not added twice).")


def find_invoice(invoice_number: str) -> dict:
    """Everything about one invoice number: is the invoice itself in the inbox, and which emails or
    statements mention it (reminders, remittances). Matches exactly, then ignoring prefixes / leading zeros."""
    from .rules import loose_invoice, norm_invoice
    n, l = norm_invoice(invoice_number), loose_invoice(invoice_number)
    exact = _q("SELECT *, 'exact' AS match FROM invoices WHERE invoice_norm=?", (n,))
    likely = [] if exact else _q("SELECT *, 'likely (prefix/leading zeros ignored)' AS match FROM invoices "
                                 "WHERE invoice_loose=?", (l,))
    mentions = []
    for r in _q("SELECT r.message_id, e.sent_at, e.subject, r.email_type, r.chase_level, r.referenced_invoices "
                "FROM email_results r JOIN emails e USING(message_id) WHERE r.referenced_invoices LIKE ?",
                (f"%{invoice_number[-4:]}%",)):
        if any(norm_invoice(x) in (n,) or loose_invoice(x) == l for x in json.loads(r["referenced_invoices"] or "[]")):
            r.pop("referenced_invoices")
            mentions.append(r)
    docs = []
    for r in _q("SELECT d.sha256, d.doc_type, f.sample_name, d.referenced_invoices FROM doc_results d "
                "JOIN files f USING(sha256) WHERE d.referenced_invoices LIKE ?", (f"%{invoice_number[-4:]}%",)):
        if any(norm_invoice(x) == n or loose_invoice(x) == l for x in json.loads(r["referenced_invoices"] or "[]")):
            r.pop("referenced_invoices")
            docs.append(r)
    return dict(query=invoice_number, invoice_documents=exact or likely, mentioned_in_emails=mentions,
                listed_in_documents=docs,
                verdict=("invoice received" if exact else "probably received (number written differently)" if likely
                         else "invoice document not found in the inbox"))


def payment_chasers(vendor: str = None, min_level: str = "reminder", detail: bool = False, limit: int = 40) -> dict:
    """Vendors chasing payment: highest level reached (reminder < escalation < final_notice) and, for the invoices
    they chase, how many are in the inbox. Summary by default; detail=true (or a vendor) adds each vendor's
    timeline of chase emails and the individual invoice numbers."""
    floor = LEVEL_ORDER.get(min_level, 1)
    where, args = ["max_level_n >= ?"], [floor]
    if vendor:
        where.append("(vendor_name LIKE ? OR vendor_key LIKE ?)")
        args += [f"%{vendor}%"] * 2
    vendors = _q(f"SELECT * FROM vendor_chasers WHERE {' AND '.join(where)} ORDER BY max_level_n DESC, chase_emails DESC",
                 args)
    by_level = dict(Counter(v["max_level"] for v in vendors))
    if not (detail or vendor):
        summary = []
        for v in vendors[:limit]:
            s = _one("SELECT count(DISTINCT upper(referenced)) AS chased, "
                     "count(DISTINCT CASE WHEN match_status='found' THEN upper(referenced) END) AS found "
                     "FROM chase_invoice_matches WHERE vendor_key=?", (v["vendor_key"],))
            summary.append(dict(vendor=v["vendor_name"], highest_level=v["max_level"], chase_emails=v["chase_emails"],
                                first=v["first_chase"][:10], last=v["last_chase"][:10], unsure_emails=v["unsure_emails"],
                                invoices_chased=s["chased"], invoices_found_in_inbox=s["found"]))
        return dict(vendors_chasing=len(vendors), by_highest_level=by_level, shown=len(summary), vendors=summary,
                    note="Summary. Ask with a vendor name (or detail=true) for the timeline and invoice numbers. "
                         "'Found' = an invoice with that number is in the inbox; the rest are not in this extract, "
                         "which starts 24 Aug 2026, so older invoices can legitimately be missing.")
    for v in vendors:
        v["timeline"] = _q("SELECT e.sent_at, r.chase_level, e.subject, r.amount_mentioned, r.message_id, "
                           "round(r.final_confidence,2) AS confidence FROM email_results r JOIN emails e "
                           "USING(message_id) WHERE r.vendor_key=? AND r.chase_level != 'none' AND "
                           "e.direction='inbound' ORDER BY e.sent_at", (v["vendor_key"],))
        inv = _q("SELECT max(m.referenced) AS invoice, m.match_status, max(m.matched_number) AS matched_as, "
                 "max(m.matched_message) AS invoice_email, count(*) AS times_chased, min(e.sent_at) AS first_chased, "
                 "max(e.sent_at) AS last_chased, max(m.note) AS note FROM chase_invoice_matches m "
                 "JOIN emails e USING(message_id) WHERE m.vendor_key=? "
                 "GROUP BY upper(replace(replace(replace(m.referenced,'-',''),' ',''),'#','')), m.match_status "
                 "ORDER BY times_chased DESC", (v["vendor_key"],))
        counts = Counter(i["match_status"] for i in inv)
        v["chased_invoices_summary"] = dict(unique_invoices=len(inv), found=counts.get("found", 0),
                                            likely=counts.get("likely", 0), not_found=counts.get("not_found", 0))
        v["chased_invoices"] = inv[:25]
        if len(inv) > 25:
            v["chased_invoices_note"] = f"showing 25 of {len(inv)}; use run_sql on chase_invoice_matches for all"
        v.pop("max_level_n", None)
    return dict(vendors_chasing=len(vendors), by_highest_level=by_level, vendors=vendors,
                note="A chase is an inbound email asking for payment. 'found' = an invoice with that number is in the "
                     "inbox; 'likely' = same number once prefixes/leading zeros are ignored; 'not_found' = not in this "
                     "inbox extract, which starts 24 Aug 2026, so older invoices can legitimately be missing.")


def vendor_profile(vendor: str) -> dict:
    """Everything from one vendor: emails by type, invoices and totals, chase status, problems."""
    like = f"%{vendor}%"
    return dict(
        vendor_query=vendor,
        # Spellings of one company are grouped (e.g. "X Service" / "X Services"); different companies stay apart.
        companies_matched=_q(
            "SELECT group_concat(DISTINCT vendor_name) AS names_on_invoices, count(DISTINCT nullif(invoice_norm, '')) AS "
            "distinct_invoices, (SELECT round(sum(t), 2) FROM (SELECT max(coalesce(total, amount_due)) AS t "
            "FROM invoices i2 WHERE i2.vendor_key = i.vendor_key AND invoice_norm != '' GROUP BY invoice_norm)) "
            "AS total_usd FROM invoices i WHERE vendor_name LIKE ? OR vendor_key LIKE ? GROUP BY vendor_key "
            "ORDER BY total_usd DESC", (like, like)),
        names_seen=_q("SELECT vendor_name, count(*) AS n FROM email_results WHERE vendor_name LIKE ? OR vendor_key LIKE ? "
                      "GROUP BY vendor_name ORDER BY n DESC LIMIT 10", (like, like)),
        emails_by_type=_q("SELECT email_type, chase_level, count(*) AS n FROM email_results WHERE vendor_name LIKE ? "
                          "OR vendor_key LIKE ? GROUP BY 1,2 ORDER BY n DESC", (like, like)),
        invoices=search_invoices(vendor=vendor, limit=15),
        chasing=payment_chasers(vendor=vendor)["vendors"],
    )


PROBLEM_KINDS = {
    "file_missing": ("an email lists an attachment that was not delivered", "ask for the file again (Front or vendor)"),
    "password_protected": ("the file is locked; only its name could be used", "get the password or a portal export"),
    "unsupported": ("a file type the agent cannot read", "add a reader for that type"),
    "unreadable": ("the file is damaged or could not be opened", "ask the sender for a new copy"),
    "unsure_classification": ("low confidence, or the keyword rules disagree with the model", "a person checks it"),
    "invoice_check_failed": ("invoice fields fail a check (math, dates, missing number or total)",
                             "check the invoice before paying it"),
    "chased_invoice_not_in_inbox": ("a vendor chases an invoice number that is not in this extract; it starts "
                                    "24 Aug 2026, so older invoices are expected to be missing",
                                    "look it up in the accounting system or ask the vendor for a copy"),
    "model_call_failed": ("the model call failed after retries; the item has no answer", "re-run the pipeline"),
    "answer_not_usable": ("the model's answer could not be used", "re-run the pipeline"),
}
# Kept for the audit trail, not problems: the cheap model declined and the stronger model answered.
AUDIT_ONLY = ("model_refused_fallback_used",)


def list_problems(kind: str = None, limit: int = 30) -> dict:
    """What the agent could not do or is not sure about: missing files, unreadable / password-protected files,
    unsure classifications, invoices failing checks, chased invoices not in the inbox, failed model calls."""
    skip = ",".join("?" * len(AUDIT_ONLY))
    summary = _q(f"SELECT issue_type AS kind, count(*) AS n, sum(scope='email') AS emails, "
                 f"sum(scope='file') AS documents FROM issues WHERE issue_type NOT IN ({skip}) "
                 f"GROUP BY issue_type ORDER BY n DESC", AUDIT_ONLY)
    for r in summary:
        r["meaning"], r["what_to_do"] = PROBLEM_KINDS.get(r["kind"], ("", ""))
        if r["kind"] != "unsure_classification":
            r.pop("emails"), r.pop("documents")
    rows = _q("SELECT issue_type, scope, ref, detail FROM issues " + ("WHERE issue_type=? " if kind else "")
              + "ORDER BY id LIMIT ?", ((kind, limit) if kind else (limit,)))
    audit = _q(f"SELECT issue_type AS kind, count(*) AS n FROM issues WHERE issue_type IN ({skip}) GROUP BY 1",
               AUDIT_ONLY)
    return dict(summary=summary, items=rows, audit_trail_not_problems=audit,
                note="Nothing is dropped: every item the pipeline could not read or is unsure about is listed here. "
                     "Unreadable files are counted under password_protected / unsupported.")


def accuracy_report() -> dict:
    """How accurate the classification is, and how that was measured."""
    from .db import connect
    from .evaluate import report
    con = connect()
    try:
        return report(con)
    finally:
        con.close()


def cost_report() -> dict:
    """What processing cost: one full run of the inbox, everything spent while building it (with what each part
    bought; the parts add up to the total), and tokens per step and model."""
    steps = _q("SELECT step, model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
               "sum(output_tokens) AS output_tokens, round(sum(cost_usd),4) AS cost_usd FROM llm_calls "
               "GROUP BY step, model")
    return dict(
        **_spend(),
        what_each_part_is={
            "one_full_run": "the answers in use: every unique document and every email classified once "
                            "(Haiku; Sonnet answered the 2 emails Haiku declined). This is the cost to process "
                            "this inbox again",
            "replaced_answers": "answers from the first version of the email instructions, re-done with the "
                                "second version, plus emails re-done after their scanned attachments were re-read",
            "accuracy_check": "the one-off second opinion: Sonnet re-checked a sample of documents and emails "
                              "(the email sample twice: before and after the instruction change)",
            "failed_attempts": "calls that timed out or failed and were retried; they cost almost nothing",
            "total_usd": "one_full_run + replaced_answers + accuracy_check (failed attempts are inside these)",
        },
        by_step_and_model=steps,
        still_unclassified=_one(
            "SELECT (SELECT count(*) FROM emails WHERE message_id NOT IN (SELECT message_id FROM email_results)) "
            "AS emails, (SELECT count(*) FROM files WHERE sha256 NOT IN (SELECT sha256 FROM doc_results)) AS files"),
        note="Chat questions are billed separately by the chat model (a few cents each, more later in a long "
             "session because the conversation is re-sent each turn) and are not included.")


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def run_sql(query: str, limit: int = 100) -> dict:
    """Read-only SQL (SQLite) for questions the other tools don't cover. Only SELECT/WITH.
    Main tables: emails, email_results, attachments, files, doc_results, invoices, chase_invoice_matches,
    vendor_chasers (view), issues, llm_calls; raw_frontapp_* hold the original export."""
    q = query.strip().rstrip(";")
    if not q.lower().startswith(("select", "with")):
        return dict(error="only SELECT or WITH queries are allowed")
    con = connect_readonly()
    con.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY)
    steps = [0]

    def guard():
        steps[0] += 1
        return 1 if steps[0] > 2000 else 0  # stops runaway queries (~ a few seconds)
    con.set_progress_handler(guard, 10000)
    try:
        cur = con.execute(q)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchmany(min(limit, 500))
        return dict(columns=cols, rows=[list(r) for r in rows], truncated=len(rows) == min(limit, 500))
    except sqlite3.Error as e:
        return dict(error=f"{type(e).__name__}: {e}")
    finally:
        con.close()


TOOLS = [inbox_overview, search_emails, get_email, search_invoices, find_invoice, payment_chasers,
         vendor_profile, list_problems, accuracy_report, cost_report, run_sql]

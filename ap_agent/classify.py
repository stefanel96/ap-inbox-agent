"""Step 3: classify every unique document and every email, and pull out invoice fields.

- One model call per unique file (duplicates share the answer) and one per email.
- The model answers in a fixed JSON schema (structured outputs), so every answer has the same shape.
- Code then checks the answer: invoice math and dates, agreement with the keyword rules,
  and confidence. Anything doubtful is marked `unsure` and logged as an issue.
- Re-running only processes what is missing (results are keyed by file hash / message id).

Run: python -m ap_agent.classify [docs|emails|all] [--limit N]
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from .config import BULK_MODEL, MAX_DOC_CHARS, MAX_EMAIL_CHARS, REVIEW_MODEL, UNSURE_BELOW
from .db import connect, log_issue, now
from .llm import call_json, image_block, save_call
from .rules import doc_rule, email_rule, loose_invoice, norm_invoice, vendor_key

DOC_VERSION = "doc-v1"
EMAIL_VERSION = "email-v2"  # v2: precedence rules + stricter escalation (see decisions log)
TYPES = ["invoice", "statement", "payment_reminder", "remittance", "other"]
NULLABLE = lambda t: {"type": [t, "null"]}

DEFINITIONS = """Categories (pick exactly one):
- invoice: a bill from a vendor asking to be paid for specific goods or services (sales invoice, tax invoice, bill).
- statement: a statement of account listing several invoices, payments or balances for a period (incl. aging reports).
- payment_reminder: a request to pay money that is due or overdue: reminder, past-due notice, dunning letter,
  collections letter or demand. A reminder that attaches an invoice is still a payment_reminder.
- remittance: a record that a payment was made or sent: remittance advice, payment confirmation or receipt,
  ACH / check / wire notice, "thank you for your payment".
- other: anything else (purchase orders, credit memos, order or delivery notices, quotes, lab results / COAs,
  W-9s, contracts, marketing, internal notes). Name what it is in `other_kind`."""

DOC_SYSTEM = f"""You read documents that arrived in the accounts payable (AP) inbox of a cannabis retailer.
Classify the document and extract the facts AP needs. Use only what is in the document; never guess.
If a field is not shown, return null. Dates as YYYY-MM-DD. Amounts as plain numbers without currency signs.

{DEFINITIONS}

For an invoice, fill `invoice` (the invoice number exactly as printed, dates, PO number, amounts).
For a statement, reminder or remittance, list the invoice numbers it mentions in `referenced_invoice_numbers`.
`confidence` is your probability (0 to 1) that `doc_type` is right; be honest, use lower values when the
document is unclear, partial or could fit two categories. `reason`: one short sentence, at most 20 words."""

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": TYPES},
        "other_kind": NULLABLE("string"),
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "vendor_name": NULLABLE("string"),
        "invoice": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "invoice_number": NULLABLE("string"),
                        "invoice_date": NULLABLE("string"),
                        "due_date": NULLABLE("string"),
                        "po_number": NULLABLE("string"),
                        "currency": NULLABLE("string"),
                        "subtotal": NULLABLE("number"),
                        "tax": NULLABLE("number"),
                        "total": NULLABLE("number"),
                        "amount_due": NULLABLE("number"),
                        "bill_to": NULLABLE("string"),
                    },
                    "required": ["invoice_number", "invoice_date", "due_date", "po_number", "currency",
                                 "subtotal", "tax", "total", "amount_due", "bill_to"],
                    "additionalProperties": False,
                },
            ]
        },
        "referenced_invoice_numbers": {"type": "array", "items": {"type": "string"}},
        "document_total": NULLABLE("number"),
    },
    "required": ["doc_type", "other_kind", "confidence", "reason", "vendor_name", "invoice",
                 "referenced_invoice_numbers", "document_total"],
    "additionalProperties": False,
}

EMAIL_SYSTEM = f"""You read emails from the accounts payable (AP) inbox of a cannabis retailer.
Decide what the email is, whether a vendor is chasing payment, and how far the chasing has gone.
Use the attachment summary: an email whose main content is an attached invoice is an invoice email.

{DEFINITIONS}

When an email fits several categories, the first matching rule wins:
1. remittance: it confirms a payment was made, sent or received (even if it lists invoices).
2. payment_reminder: it asks the retailer to pay an amount that is due or overdue (past-due notice, dunning,
   collections, "please remit", account on hold), even if an invoice or statement is attached.
   NOT a reminder: delivery or shipping notices that only mention collecting payment / COD on delivery or a
   past-due balance in passing; notices of a future automatic charge (autopay, upcoming billing date).
3. statement: its main content is a statement of account or aging list, without an explicit request to pay.
4. invoice: its main content is one or more invoices (attached or in the body), including shipping or order
   confirmations whose attachment is an invoice, and AP forwards that carry only an invoice.
5. other: everything else (order or shipping confirmations without an invoice, purchase orders, autopay notices,
   marketing, internal notes).

chase_level (only for a vendor, or a collection agency acting for one, asking the retailer to pay):
- none: not asking for payment, or written / forwarded by the AP team without a vendor's payment demand.
- reminder: a routine request to pay due or overdue amounts, including statements that ask to pay overdue balances.
  Listing several overdue invoices, or saying "past due", is still a reminder.
- escalation: only with an explicit stronger step: second or third notice, late fees or interest charged,
  account or credit hold, shipments stopped or COD required because of non-payment, management involved,
  or a firm deadline with consequences.
- final_notice: explicit final notice or last warning, disconnection notice, referral to collections or legal,
  credit reporting, or a message from a collection agency.

`vendor_name` is the company the email is about (the original sender of a forwarded email; for a collection
agency, the creditor it collects for). List invoice numbers the email mentions in `referenced_invoice_numbers`.
`confidence` is your probability (0 to 1) that `email_type` and `chase_level` are both right.
`reason`: one short sentence, at most 20 words."""

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "email_type": {"type": "string", "enum": TYPES},
        "chase_level": {"type": "string", "enum": ["none", "reminder", "escalation", "final_notice"]},
        "vendor_name": NULLABLE("string"),
        "referenced_invoice_numbers": {"type": "array", "items": {"type": "string"}},
        "amount_mentioned": NULLABLE("number"),
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["email_type", "chase_level", "vendor_name", "referenced_invoice_numbers", "amount_mentioned",
                 "confidence", "reason"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- documents

def _doc_content(f, ctx):
    head = (f"File name: {f['sample_name']}\nArrived with email from {ctx['from_email']} "
            f"subject \"{ctx['subject']}\" on {ctx['sent_at'][:10]}.\n")
    if f["read_method"] == "vision":
        blocks = [image_block(p) for p in json.loads(f["image_paths"])]
        note = f"The document is a scan/image; {len(blocks)} page image(s) follow"
        if f["pages"] and f["pages"] > len(blocks):
            note += f" (of {f['pages']} pages; the rest were not sent)"
        return blocks + [{"type": "text", "text": head + note + "."}]
    text = f["text"]
    cut = len(text) > MAX_DOC_CHARS
    body = text[:MAX_DOC_CHARS] + ("\n[... text cut for length ...]" if cut else "")
    return [{"type": "text", "text": f"{head}\nExtracted text:\n{body}"}]


def _check_invoice(inv):
    notes = []
    if not inv.get("invoice_number"):
        notes.append("no invoice number")
    if inv.get("total") is None and inv.get("amount_due") is None:
        notes.append("no total or amount due")
    dates = {}
    for k in ("invoice_date", "due_date"):
        v = inv.get(k)
        if v:
            try:
                dates[k] = date.fromisoformat(v)
            except ValueError:
                notes.append(f"{k} not a valid date: {v}")
    if "invoice_date" in dates and "due_date" in dates and dates["due_date"] < dates["invoice_date"]:
        notes.append("due date before invoice date")
    sub, tax, tot, due = inv.get("subtotal"), inv.get("tax"), inv.get("total"), inv.get("amount_due")
    if sub is not None and tax is not None and tot is not None:
        if abs(sub + tax - tot) > max(0.02, 0.005 * abs(tot)):
            notes.append(f"subtotal {sub} + tax {tax} != total {tot}")
    if due is not None and tot is not None and due > tot + 0.01:
        notes.append(f"amount due {due} > total {tot}")
    if tot is not None and tot < 0:
        notes.append("negative total (credit?)")
    return notes


def classify_docs(con, limit=None):
    files = con.execute("SELECT * FROM files WHERE sha256 NOT IN (SELECT sha256 FROM doc_results)").fetchall()
    if limit:
        files = files[:limit]
    ctx = {}
    for r in con.execute("SELECT a.sha256, e.from_email, e.subject, e.sent_at FROM attachments a "
                         "JOIN emails e USING(message_id) WHERE a.sha256 IS NOT NULL ORDER BY e.sent_at"):
        ctx.setdefault(r["sha256"], dict(r))
    readable = [f for f in files if f["read_method"] in ("text", "vision") and f["status"] != "empty"]
    readable_ids = {f["sha256"] for f in readable}
    unreadable = [f for f in files if f["sha256"] not in readable_ids]

    for f in unreadable:  # nothing for the model to read: say so, guess only from the file name
        guess = doc_rule(f["sample_name"], "")
        kind = "zip_container" if f["kind"] == "zip" else None
        con.execute(
            "INSERT OR REPLACE INTO doc_results(sha256, doc_type, other_kind, confidence, rule_type, agrees_with_rule,"
            " final_confidence, unsure, reason, model, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["sha256"], "other" if kind else "unreadable", kind, 0.0, guess, None, 0.0, 0 if kind else 1,
             f"not read: {f['detail']}" + (f"; file name suggests {guess}" if guess else ""), None, DOC_VERSION, now()))
    print(f"documents to classify: {len(readable)} (+{len(unreadable)} recorded as unreadable)")

    def work(f):
        c = ctx.get(f["sha256"], {"from_email": "?", "subject": "?", "sent_at": "????-??-??"})
        vision = f["read_method"] == "vision"
        return f, *call_json("classify_doc", f["sha256"], BULK_MODEL, DOC_SYSTEM, _doc_content(f, c), DOC_SCHEMA,
                             max_tokens=6000, timeout=180 if vision else 60, fallback_model=REVIEW_MODEL)

    _run(con, readable, work, _save_doc)


def _save_doc(con, f, ans):
    rule = doc_rule(f["sample_name"], f["text"])
    conf = max(0.0, min(1.0, float(ans["confidence"])))
    agrees = None if rule is None else int(rule == ans["doc_type"])
    final = conf * (0.8 if agrees == 0 else 1.0)
    if f["status"] == "scanned" and f["pages"] and f["pages"] > 2:
        final *= 0.95  # the model only saw the first pages
    unsure = int(final < UNSURE_BELOW)
    vendor = ans.get("vendor_name")
    con.execute(
        "INSERT OR REPLACE INTO doc_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f["sha256"], ans["doc_type"], ans.get("other_kind"), conf, rule, agrees, final, unsure,
         ans.get("reason"), vendor, json.dumps(ans.get("referenced_invoice_numbers") or []),
         ans.get("document_total"), json.dumps(ans), BULK_MODEL, DOC_VERSION, now()))
    if unsure:
        log_issue(con, "classify", "file", f["sha256"], "unsure_classification",
                  f"{f['sample_name']}: {ans['doc_type']} at {final:.2f} (rule said {rule})")
    inv = ans.get("invoice")
    if ans["doc_type"] == "invoice":
        if not inv:
            log_issue(con, "classify", "invoice", f["sha256"], "invoice_without_fields", f["sample_name"])
            return
        notes = _check_invoice(inv)
        first = con.execute("SELECT a.message_id, e.sent_at FROM attachments a JOIN emails e USING(message_id) "
                            "WHERE a.sha256=? ORDER BY e.sent_at LIMIT 1", (f["sha256"],)).fetchone()
        con.execute(
            "INSERT OR REPLACE INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["sha256"], vendor, vendor_key(vendor), inv.get("invoice_number"), norm_invoice(inv.get("invoice_number")),
             loose_invoice(inv.get("invoice_number")), inv.get("invoice_date"), inv.get("due_date"),
             inv.get("po_number"), inv.get("currency"), inv.get("subtotal"), inv.get("tax"), inv.get("total"),
             inv.get("amount_due"), inv.get("bill_to"), int(not notes), "; ".join(notes),
             first["message_id"] if first else None, first["sent_at"] if first else None))
        if notes:
            log_issue(con, "classify", "invoice", f["sha256"], "invoice_check_failed",
                      f"{f['sample_name']}: {'; '.join(notes)}")


# ---------------------------------------------------------------- emails

def _email_content(e, atts):
    lines = [f"From: {e['from_name'] or ''} <{e['from_email']}>", f"To: {e['to_emails']}",
             f"Reply-To: {e['reply_to']}" if e["reply_to"] else None, f"Date: {e['sent_at']}",
             f"Direction: {e['direction']} ({'received by' if e['direction'] == 'inbound' else 'sent or forwarded by'} AP)",
             f"Subject: {e['subject']}", f"Thread subject: {e['thread_subject']}"]
    if atts:
        lines.append("Attachments:")
        for a in atts:
            if a["status"] in ("missing", "missing_inline_image"):
                lines.append(f"- {a['filename']} [{a['status']}: not available]")
            else:
                summary = a["doc_type"] or "not classified"
                if a["inv_no"]:
                    summary += f", invoice {a['inv_no']}"
                if a["vendor"]:
                    summary += f", vendor {a['vendor']}"
                lines.append(f"- {a['filename']} -> {summary}")
    body = e["body"] or ""
    cut = len(body) > MAX_EMAIL_CHARS
    lines.append("\nBody:\n" + body[:MAX_EMAIL_CHARS] + ("\n[... cut for length ...]" if cut else ""))
    return [{"type": "text", "text": "\n".join(x for x in lines if x is not None)}]


def classify_emails(con, limit=None):
    emails = con.execute("SELECT * FROM emails WHERE message_id NOT IN (SELECT message_id FROM email_results) "
                         "ORDER BY sent_at").fetchall()
    if limit:
        emails = emails[:limit]
    atts = {}
    for a in con.execute("SELECT a.message_id, a.filename, a.status, d.doc_type, d.vendor_name AS vendor, "
                         "i.invoice_number AS inv_no FROM attachments a LEFT JOIN doc_results d USING(sha256) "
                         "LEFT JOIN invoices i USING(sha256) WHERE a.status != 'missing_inline_image'"):
        atts.setdefault(a["message_id"], []).append(a)
    print(f"emails to classify: {len(emails)}")

    def work(e):
        return e, *call_json("classify_email", e["message_id"], BULK_MODEL, EMAIL_SYSTEM,
                             _email_content(e, atts.get(e["message_id"], [])), EMAIL_SCHEMA,
                             max_tokens=6000, fallback_model=REVIEW_MODEL)

    _run(con, emails, work, _save_email)


def _save_email(con, e, ans):
    rtype, rchase = email_rule(e["subject"], e["body"])
    conf = max(0.0, min(1.0, float(ans["confidence"])))
    agrees = None if rtype is None else int(rtype == ans["email_type"])
    final = conf * (0.8 if agrees == 0 else 1.0)
    if (rchase == "none") != (ans["chase_level"] == "none"):
        final *= 0.85  # rules and model disagree on whether this is a payment chase at all
    unsure = int(final < UNSURE_BELOW)
    vendor = ans.get("vendor_name")
    con.execute(
        "INSERT OR REPLACE INTO email_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (e["message_id"], ans["email_type"], ans["chase_level"], vendor, vendor_key(vendor, e["from_domain"]),
         json.dumps(ans.get("referenced_invoice_numbers") or []), ans.get("amount_mentioned"), conf, rtype, rchase,
         agrees, final, unsure, ans.get("reason"), json.dumps(ans), BULK_MODEL, EMAIL_VERSION, now()))
    if unsure:
        log_issue(con, "classify", "email", e["message_id"], "unsure_classification",
                  f"\"{e['subject']}\": {ans['email_type']}/{ans['chase_level']} at {final:.2f} (rules: {rtype}/{rchase})")


# ---------------------------------------------------------------- shared runner

def _run(con, items, work, save, workers=int(os.environ.get("AP_WORKERS", "48"))):
    done = failed = 0
    spent = 0.0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, it) for it in items]
        for fut in as_completed(futures):
            item, ans, rec = fut.result()
            save_call(con, rec)
            spent += rec["cost_usd"]
            if ans is None:
                failed += 1
                ref = rec["ref"]
                log_issue(con, "classify", "llm", ref, "model_call_failed", rec["error"] or "unknown error")
            else:
                if rec.get("error"):  # answered, but only by the fallback model after a refusal
                    log_issue(con, "classify", "llm", rec["ref"], "model_refused_fallback_used", rec["error"])
                try:
                    save(con, item, ans)
                except Exception as ex:  # a malformed answer is logged, not dropped
                    failed += 1
                    log_issue(con, "classify", "llm", rec["ref"], "answer_not_usable", f"{type(ex).__name__}: {ex}")
            done += 1
            if done % 100 == 0:
                con.commit()
                print(f"  {done}/{len(items)}  failed {failed}  cost so far ${spent:.2f}", flush=True)
    con.commit()
    print(f"  finished {done}  failed {failed}  cost ${spent:.2f}")


def main():
    args = sys.argv[1:]
    what = args[0] if args and not args[0].startswith("--") else "all"
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    con = connect()
    if what in ("docs", "all"):
        classify_docs(con, limit)
    if what in ("emails", "all"):
        classify_emails(con, limit)


if __name__ == "__main__":
    main()

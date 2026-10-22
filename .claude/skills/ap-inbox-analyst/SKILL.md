---
name: ap-inbox-analyst
description: Answer questions about the accounts payable inbox (invoices, statements, payment reminders, remittances, vendors chasing payment) using the ap_inbox MCP tools, with sources and stated uncertainty.
version: 1.1.0
metadata:
  hermes:
    tags: [accounts-payable, finance, inbox]
    category: finance
---

# AP inbox analyst

## When to use
Any question about the AP inbox: what arrived, invoices and amounts, vendors, who is chasing payment and how
hard, whether a specific invoice was received, what could not be read, how accurate the results are, or what
processing cost.

## Procedure
1. **Pick the narrowest tool** (see AGENTS.md table). For a vendor question start with `vendor_profile`; for
   "who is chasing us" use `payment_chasers` (summary first; a vendor name or `detail=true` for timelines);
   for one invoice number use `find_invoice`.
2. **Check coverage before totals.** If the answer is a count or a sum, note what is excluded only when it
   matters: missing attachments, unreadable files, unsure items, documents without an invoice number.
3. **Answer, then evidence.** One-sentence answer first. Then a short table (about 8 rows) with dates,
   message ids and amounts. Offer the rest instead of listing it.
4. **State uncertainty once.** If an item you rely on is `unsure`, or a chased invoice is `likely` rather than
   `found`, say so in a line.
5. **Suggest 2–3 next questions** under "You could ask:".
6. **For "how accurate are you"**: call `accuracy_report`, give the agreement per area (documents, emails,
   chase level) with the 95% range, and say in one sentence how it was measured and what it is not (agreement
   with a stronger model on a sample, not verified accuracy; no hand labels yet).
7. **For anything the tools don't cover**: use `run_sql` with a SELECT query. Prefer the summary tables
   (`email_results`, `invoices`, `doc_results`, `vendor_chasers`, `chase_invoice_matches`, `issues`). To count
   invoices, count distinct vendor + invoice number (`vendor_key`, `invoice_norm`), not files.

## Payment chasing, in brief
- `reminder`: a polite or routine request to pay (statement with overdue items, "friendly reminder").
- `escalation`: more pressure: repeated follow-ups, late fees, account or credit hold, "urgent".
- `final_notice`: the last step: collections, legal action, credit reporting, service disconnection, or a
  collection agency. These are the ones to pay or resolve first.
A vendor's level is the highest one reached in the period; the timeline shows how it got there.

## Example questions
- "Which vendors are chasing us, and how far did they go?"
- "Show the final notices and whether the invoices they mention were received."
- "Is invoice <number> in the inbox? Has anyone chased it?"
- "Total invoiced by <vendor> in September?"
- "What couldn't you read, and what should we do about it?"
- "How accurate is your classification, and how do you know?"
- "Does this email look legitimate?"

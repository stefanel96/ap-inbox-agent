"""SQLite schema and connections.

The agent's tools open the database read-only; only the pipeline steps write to it.
"""
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    message_id      TEXT PRIMARY KEY,   -- Front id, e.g. msg_2qs1sqxg
    row_id          TEXT,
    conversation_id TEXT,
    thread_subject  TEXT,
    sent_at         TEXT,               -- UTC, ISO 8601
    direction       TEXT,               -- inbound | outbound (sent or forwarded by the AP team)
    event_type      TEXT,
    from_name       TEXT,
    from_email      TEXT,
    from_domain     TEXT,
    to_emails       TEXT,
    reply_to        TEXT,
    subject         TEXT,
    body            TEXT,
    body_source     TEXT,               -- text | html (when the plain-text part was empty or a footer)
    n_attachments   INTEGER
);

CREATE TABLE IF NOT EXISTS attachments (
    message_id     TEXT,
    attachment_id  TEXT,                -- Front file id, e.g. fil_4xydxys4 (zip members: fil_x#member.pdf)
    filename       TEXT,
    content_type   TEXT,
    size_declared  INTEGER,
    is_inline      INTEGER,
    disk_path      TEXT,
    sha256         TEXT,
    status         TEXT,                -- ok | missing | missing_inline_image | from_zip
    detail         TEXT,
    PRIMARY KEY (message_id, attachment_id)
);

CREATE TABLE IF NOT EXISTS files (       -- one row per unique file content
    sha256       TEXT PRIMARY KEY,
    sample_name  TEXT,
    copies       INTEGER,
    kind         TEXT,                  -- pdf | image | xlsx | docx | csv | text | ics | json | html | eml | zip | unknown
    size         INTEGER,
    pages        INTEGER,
    text         TEXT,
    text_chars   INTEGER,
    read_method  TEXT,                  -- text | vision | none
    status       TEXT,                  -- ok | scanned | password_protected | unreadable | unsupported | empty
    detail       TEXT,
    image_paths  TEXT                   -- JSON list of rendered page images (scans, images)
);

CREATE TABLE IF NOT EXISTS doc_results (
    sha256          TEXT PRIMARY KEY,
    doc_type        TEXT,               -- invoice | statement | payment_reminder | remittance | other | unreadable
    other_kind      TEXT,
    confidence      REAL,
    rule_type       TEXT,               -- what the keyword rules said (second opinion)
    agrees_with_rule INTEGER,
    final_confidence REAL,
    unsure          INTEGER,
    reason          TEXT,
    vendor_name     TEXT,
    referenced_invoices TEXT,           -- JSON list (statements, reminders, remittances)
    doc_total       REAL,
    raw_json        TEXT,
    model           TEXT,
    prompt_version  TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
    sha256           TEXT PRIMARY KEY,
    vendor_name      TEXT,
    vendor_key       TEXT,
    invoice_number   TEXT,
    invoice_norm     TEXT,
    invoice_loose    TEXT,
    invoice_date     TEXT,
    due_date         TEXT,
    po_number        TEXT,
    currency         TEXT,
    subtotal         REAL,
    tax              REAL,
    total            REAL,
    amount_due       REAL,
    bill_to          TEXT,
    checks_passed    INTEGER,
    check_notes      TEXT,
    first_seen_message TEXT,
    first_seen_at    TEXT
);

CREATE TABLE IF NOT EXISTS email_results (
    message_id      TEXT PRIMARY KEY,
    email_type      TEXT,
    chase_level     TEXT,               -- none | reminder | escalation | final_notice
    vendor_name     TEXT,
    vendor_key      TEXT,
    referenced_invoices TEXT,           -- JSON list
    amount_mentioned REAL,
    confidence      REAL,
    rule_type       TEXT,
    rule_chase      TEXT,
    agrees_with_rule INTEGER,
    final_confidence REAL,
    unsure          INTEGER,
    reason          TEXT,
    raw_json        TEXT,
    model           TEXT,
    prompt_version  TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS chase_invoice_matches (
    vendor_key      TEXT,
    message_id      TEXT,
    referenced      TEXT,
    match_status    TEXT,               -- found | likely | not_found
    matched_sha256  TEXT,
    matched_number  TEXT,
    matched_message TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    step        TEXT,
    scope       TEXT,                   -- email | attachment | file | invoice | chase | llm
    ref         TEXT,
    issue_type  TEXT,
    detail      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    step          TEXT,
    ref           TEXT,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_read    INTEGER,
    cache_write   INTEGER,
    cost_usd      REAL,
    ok            INTEGER,
    error         TEXT,
    at            TEXT
);

CREATE TABLE IF NOT EXISTS review_results (   -- second-opinion sample for the accuracy estimate
    scope       TEXT,                   -- doc | email
    ref         TEXT,
    review_type TEXT,
    review_chase TEXT,
    review_json TEXT,
    model       TEXT,
    PRIMARY KEY (scope, ref)
);

CREATE TABLE IF NOT EXISTS labels (             -- hand labels (gold), optional
    scope   TEXT,
    ref     TEXT,
    label   TEXT,
    chase   TEXT,
    note    TEXT,
    at      TEXT,
    PRIMARY KEY (scope, ref)
);

CREATE INDEX IF NOT EXISTS ix_att_sha ON attachments(sha256);
CREATE INDEX IF NOT EXISTS ix_att_msg ON attachments(message_id);
CREATE INDEX IF NOT EXISTS ix_email_from ON emails(from_domain);
CREATE INDEX IF NOT EXISTS ix_inv_norm ON invoices(invoice_norm);
CREATE INDEX IF NOT EXISTS ix_inv_loose ON invoices(invoice_loose);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def connect_readonly(path=DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def log_issue(con, step, scope, ref, issue_type, detail=""):
    con.execute(
        "INSERT INTO issues(step, scope, ref, issue_type, detail, created_at) VALUES (?,?,?,?,?,?)",
        (step, scope, ref, issue_type, detail, now()),
    )

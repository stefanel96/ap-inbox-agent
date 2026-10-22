"""Fast tests for the deterministic parts (no data, no API calls). Run: pytest -q"""
import sqlite3

import pytest

from ap_agent import tools
from ap_agent.classify import _check_invoice
from ap_agent.load import copy_unescape, html_to_text
from ap_agent.rules import doc_rule, email_rule, loose_invoice, norm_invoice, vendor_key


def test_copy_unescape():
    assert copy_unescape("\\N") is None
    assert copy_unescape("a\\tb\\nc") == "a\tb\nc"
    assert copy_unescape("x\\\\y") == "x\\y"
    assert copy_unescape("\\101") == "A"
    assert copy_unescape("plain") == "plain"


def test_html_to_text():
    assert html_to_text("<p>Hello<br>World</p><style>x{}</style>") .split() == ["Hello", "World"]


@pytest.mark.parametrize("raw,norm,loose", [
    ("INV-00123 ", "INV00123", "123"),
    ("SI-624807", "SI624807", "624807"),
    ("#0042", "0042", "42"),
    ("", "", ""),
])
def test_invoice_number_keys(raw, norm, loose):
    assert norm_invoice(raw) == norm
    assert loose_invoice(raw) == loose


def test_vendor_key_groups_spellings():
    assert vendor_key("Acme Holdings, Inc.") == vendor_key("Acme") == "acme"
    assert vendor_key("Acme Wholesale, LLC") == vendor_key("ACME") == "acme"
    assert vendor_key("Bolt Sales & Service") == vendor_key("Bolt Sales & Services")
    assert vendor_key(None, "Vendor.com") == "vendor.com"


@pytest.mark.parametrize("filename,expected", [
    ("Inv_50703_from_Some_Vendor.pdf", "invoice"),
    ("Statement of Account Sept.pdf", "statement"),
    ("Remittance Advice 123.pdf", "remittance"),
    ("Final notice.pdf", "payment_reminder"),
    ("scan_0001.pdf", None),            # no keyword: the rules have no opinion
])
def test_doc_rule(filename, expected):
    assert doc_rule(filename, "") == expected


@pytest.mark.parametrize("subject,expected", [
    ("FINAL NOTICE - account referred to collections", ("payment_reminder", "final_notice")),
    ("Second notice: invoice 123 past due", ("payment_reminder", "escalation")),
    ("Friendly reminder: payment due", ("payment_reminder", "reminder")),
    ("Invoice 1551 from Some Vendor LLC", ("invoice", "none")),
])
def test_email_rule(subject, expected):
    assert email_rule(subject, "") == expected


def test_invoice_checks():
    good = dict(invoice_number="A1", invoice_date="2026-09-01", due_date="2026-10-01", subtotal=100.0, tax=8.0,
                total=108.0, amount_due=108.0)
    assert _check_invoice(good) == []
    assert any("!= total" in n for n in _check_invoice({**good, "total": 120.0}))
    assert "due date before invoice date" in _check_invoice({**good, "due_date": "2026-08-01"})
    assert "no invoice number" in _check_invoice({**good, "invoice_number": None})
    assert any("not a valid date" in n for n in _check_invoice({**good, "invoice_date": "09/01/2026"}))


@pytest.fixture
def tiny_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE emails (message_id TEXT, subject TEXT)")
    con.execute("INSERT INTO emails VALUES ('msg_1', 'hello')")
    con.commit()
    con.close()

    def ro():
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(tools, "connect_readonly", ro)
    return path


def test_run_sql_is_read_only(tiny_db):
    assert tools.run_sql("SELECT count(*) FROM emails")["rows"] == [[1]]
    assert "error" in tools.run_sql("DELETE FROM emails")
    assert "error" in tools.run_sql("WITH x AS (SELECT 1) DELETE FROM emails")   # disguised write
    assert "error" in tools.run_sql("SELECT 1; DROP TABLE emails")
    assert tools.run_sql("SELECT count(*) FROM emails")["rows"] == [[1]]          # still intact

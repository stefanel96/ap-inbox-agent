"""Keyword rules: a cheap, independent second opinion next to the model.

The model decides; the rules never override it. When the two disagree, the result is
marked as less certain, and the disagreement rate is one of the accuracy signals.
"""
import re

TYPES = ["invoice", "statement", "payment_reminder", "remittance", "other"]

_DOC_RULES = [
    ("remittance", r"remit(tance)?|payment (advice|confirmation|receipt|notification)|\bach\b|eft notice|paid in full|"
                   r"thank you for your payment|payment (sent|received|processed)"),
    ("payment_reminder", r"past[ -]?due|overdue|reminder|final notice|collections?|dunning|second notice|"
                         r"demand (letter|for payment)|delinquen"),
    ("statement", r"statement|\bsoa\b|account summary|aging|open (items|invoices)"),
    ("invoice", r"invoice|\binv[ _#-]?\d|\binv\b|bill(ing)? ?#|\bbill\b|sales invoice|tax invoice"),
    ("other", r"purchase order|\bpo[ -]?\d|\bcoa\b|certificate of analysis|w-?9|manifest|packing|"
              r"delivery|shipment|quote|estimate|credit memo|agreement|contract"),
]

_CHASE_RULES = [
    ("final_notice", r"final notice|final demand|last notice|collections?|collection agency|legal action|"
                     r"referred to|pre-?legal|credit report|impact on your business credit"),
    ("escalation", r"second notice|2nd notice|third notice|3rd notice|urgent|credit hold|on hold|"
                   r"suspend|late (fee|charge)|escalat|immediate(ly)? (payment|attention)|seriously past due"),
    ("reminder", r"reminder|past[ -]?due|overdue|outstanding (balance|invoice)|payment (is )?due|"
                 r"friendly reminder|follow(ing)? up on (payment|invoice)"),
]


def _first_match(rules, text):
    for label, pattern in rules:
        if re.search(pattern, text, re.I):
            return label
    return None


def doc_rule(filename: str, text: str):
    """Filename first (most specific), then the first part of the text."""
    # None means "no keyword matched": the rules have no opinion, which is not a disagreement.
    return _first_match(_DOC_RULES, filename or "") or _first_match(_DOC_RULES, (text or "")[:1500])


def email_rule(subject: str, body: str) -> tuple:
    head = f"{subject or ''}\n{(body or '')[:2000]}"
    chase = _first_match(_CHASE_RULES, head) or "none"
    etype = _first_match(_DOC_RULES, subject or "") or _first_match(_DOC_RULES, (body or "")[:800])
    if chase != "none" and etype in (None, "invoice", "statement", "other"):
        etype = "payment_reminder"
    return etype, chase


def norm_invoice(number: str) -> str:
    """Exact-match key: upper case, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", (number or "").upper())


def loose_invoice(number: str) -> str:
    """Looser key for 'likely the same invoice': drop common prefixes and leading zeros."""
    s = norm_invoice(number)
    s = re.sub(r"^(INVOICE|INV|NO|NUMBER|BILL|SI|SO)(?=\d)", "", s)
    return s.lstrip("0") or s


_SUFFIXES = r"\b(llc|l\.l\.c|inc|incorporated|co|corp|corporation|company|ltd|limited|lp|llp|pllc|the|dba)\b"


def vendor_key(name: str, domain: str = None) -> str:
    """Group different spellings of one vendor: 'Acme Wholesale, LLC' and 'ACME' -> 'acme'."""
    if name:
        s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
        s = re.sub(_SUFFIXES, " ", s)
        s = re.sub(r"\b(wholesale|distribution|distributors?|accounts receivable|ar|billing|team|holdings?|group|"
                   r"california|ca|usa|us|of america|services?)\b", " ", s)
        s = " ".join(s.split())
        if s:
            return s
    return (domain or "unknown").lower()

"""Hand-label a sample: the ground truth for the accuracy report. The agent's answers stay hidden while you label.

Two sets:
  random  (default 30): 20 emails + 10 documents picked at random -> unbiased overall accuracy
  chases  (default 20): emails the agent flagged as payment chases -> is the chase (and its level) real?

Run:  python -m ap_agent.label --set random
      python -m ap_agent.label --set chases
Keys: type   1 invoice  2 statement  3 payment_reminder  4 remittance  5 other
      chase  0 none  1 reminder  2 escalation  3 final_notice      (emails only)
      m = show more text   s = skip (can't tell)   q = save and quit (you can continue later)
Scans: open the printed .jpg path (a picture we rendered; never open the original PDF from the inbox).
"""
import json
import random
import sys

from .db import connect, now

TYPES = {"1": "invoice", "2": "statement", "3": "payment_reminder", "4": "remittance", "5": "other"}
CHASE = {"0": "none", "1": "reminder", "2": "escalation", "3": "final_notice"}
SEED = 2026


def _ask(prompt, allowed, more=None):
    while True:
        a = input(prompt).strip().lower()
        if a == "m" and more:
            more()
            continue
        if a in allowed or a in ("s", "q"):
            return a


def _show_email(con, r, chars=1500):
    print(f"FROM: {r['from_name'] or ''} <{r['from_email']}>   TO: {r['to_emails']}")
    print(f"DATE: {r['sent_at']}   DIRECTION: {r['direction']} ({'received' if r['direction'] == 'inbound' else 'sent/forwarded by AP'})")
    print(f"SUBJECT: {r['subject']}")
    atts = con.execute("SELECT a.filename, a.status, f.read_method, f.text, f.image_paths FROM attachments a "
                       "LEFT JOIN files f USING(sha256) WHERE a.message_id=? AND a.status != 'missing_inline_image'",
                       (r["message_id"],)).fetchall()
    if atts:
        print("ATTACHMENTS:")
        for a in atts:
            line = f"  - {a['filename']} [{a['status']}]"
            if a["text"]:
                line += "  | " + " ".join(a["text"][:160].split())
            elif a["image_paths"]:
                line += f"  | scan, see {json.loads(a['image_paths'])[0]}"
            print(line)
    print("BODY:\n" + (r["body"] or "")[:chars])


def _show_doc(r, chars=1500):
    print(f"FILE: {r['sample_name']}   ({r['kind']}, read as {r['read_method']}, {r['pages'] or '?'} pages)")
    if r["read_method"] == "vision" and r["image_paths"]:
        for p in json.loads(r["image_paths"]):
            print(f"  scan page image: {p}")
    print((r["text"] or "")[:chars])


def main():
    args = sys.argv[1:]
    which = args[args.index("--set") + 1] if "--set" in args else "random"
    n = int(args[args.index("--n") + 1]) if "--n" in args else (30 if which == "random" else 20)
    con = connect()
    done = {(r[0], r[1]) for r in con.execute("SELECT scope, ref FROM labels")}
    rnd = random.Random(SEED + (0 if which == "random" else 1))
    if which == "chases":
        emails = con.execute("SELECT e.* FROM emails e JOIN email_results r USING(message_id) "
                             "WHERE r.chase_level != 'none' AND e.direction='inbound' ORDER BY e.message_id").fetchall()
        sample = [("email", e) for e in rnd.sample(emails, min(n, len(emails)))]
    else:
        emails = con.execute("SELECT * FROM emails ORDER BY message_id").fetchall()
        docs = con.execute("SELECT f.* FROM files f JOIN doc_results d USING(sha256) WHERE d.model IS NOT NULL "
                           "ORDER BY f.sha256").fetchall()
        n_docs = n // 3
        sample = [("email", e) for e in rnd.sample(emails, n - n_docs)] + [("doc", d) for d in rnd.sample(docs, n_docs)]
        rnd.shuffle(sample)
    print(__doc__)
    labeled = 0
    for i, (scope, r) in enumerate(sample, 1):
        ref = r["message_id"] if scope == "email" else r["sha256"]
        if (scope, ref) in done:
            continue
        print("\n" + "=" * 90 + f"\n[{i}/{len(sample)}] {scope.upper()}   (set: {which})")
        if scope == "email":
            _show_email(con, r)
            more = lambda: _show_email(con, r, chars=6000)
        else:
            _show_doc(r)
            more = lambda: _show_doc(r, chars=6000)
        t = _ask("type [1-5, m, s, q]: ", TYPES, more)
        if t == "q":
            break
        if t == "s":
            continue
        chase = None
        if scope == "email":
            c = _ask("chase level [0-3, m, s, q]: ", CHASE, more)
            if c == "q":
                break
            if c == "s":
                continue
            chase = CHASE[c]
        note = input("note (optional, Enter to skip): ").strip()
        con.execute("INSERT OR REPLACE INTO labels VALUES (?,?,?,?,?,?)",
                    (scope, ref, TYPES[t], chase, f"{which}: {note}" if note else which, now()))
        con.commit()
        labeled += 1
    print(f"\nsaved {labeled} labels. See the result: python -m ap_agent.evaluate report")


if __name__ == "__main__":
    main()

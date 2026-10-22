"""Step 5: estimate how accurate the classification is.

Three signals, from weakest to strongest:
1. Agreement with the keyword rules (free, on everything; rough).
2. An independent second opinion: a stronger model (REVIEW_MODEL) re-reads a random sample from
   scratch, without seeing the first answer. The sample is stratified per class, and the overall number
   is re-weighted by how common each class is, so rare classes are measured without skewing the total.
3. Hand labels (`labels` table), when a person has labeled a sample: the real ground truth.

It also checks calibration: is the model right more often when it says it is more confident?

Run: python -m ap_agent.evaluate review [--per-class N]   (spends money: the review sample)
     python -m ap_agent.evaluate report                   (free: prints the numbers)
"""
import json
import math
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .classify import DOC_SCHEMA, DOC_SYSTEM, EMAIL_SCHEMA, EMAIL_SYSTEM, _doc_content, _email_content
from .config import REVIEW_MODEL
from .db import connect
from .llm import call_json, save_call

SEED = 7


def wilson(k, n, z=1.96):
    """95% confidence interval for a proportion (works for small samples)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def _stratified(rows, key, per_class):
    rnd = random.Random(SEED)
    groups = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    out = []
    for k in sorted(groups):
        g = groups[k]
        rnd.shuffle(g)
        out.extend(g[:per_class])
    return out


def review(con, per_class=16):
    done = {(r[0], r[1]) for r in con.execute("SELECT scope, ref FROM review_results")}
    docs = con.execute("SELECT f.*, d.doc_type FROM files f JOIN doc_results d USING(sha256) "
                       "WHERE d.model IS NOT NULL").fetchall()
    ctx = {}
    for r in con.execute("SELECT a.sha256, e.from_email, e.subject, e.sent_at FROM attachments a "
                         "JOIN emails e USING(message_id) WHERE a.sha256 IS NOT NULL ORDER BY e.sent_at"):
        ctx.setdefault(r["sha256"], dict(r))
    doc_sample = [d for d in _stratified(docs, "doc_type", per_class) if ("doc", d["sha256"]) not in done]

    emails = con.execute("SELECT e.*, r.email_type, r.chase_level, r.email_type || '/' || r.chase_level AS cls "
                         "FROM emails e JOIN email_results r USING(message_id)").fetchall()
    email_sample = [e for e in _stratified(emails, "cls", per_class) if ("email", e["message_id"]) not in done]
    atts = {}
    for a in con.execute("SELECT a.message_id, a.filename, a.status, d.doc_type, d.vendor_name AS vendor, "
                         "i.invoice_number AS inv_no FROM attachments a LEFT JOIN doc_results d USING(sha256) "
                         "LEFT JOIN invoices i USING(sha256) WHERE a.status != 'missing_inline_image'"):
        atts.setdefault(a["message_id"], []).append(a)

    def work(item):
        scope, obj = item
        if scope == "doc":
            c = ctx.get(obj["sha256"], {"from_email": "?", "subject": "?", "sent_at": "????-??-??"})
            ans, rec = call_json("review_doc", obj["sha256"], REVIEW_MODEL, DOC_SYSTEM, _doc_content(obj, c),
                                 DOC_SCHEMA, effort="medium", max_tokens=6000, timeout=180)
            return scope, obj["sha256"], ans, rec
        ans, rec = call_json("review_email", obj["message_id"], REVIEW_MODEL, EMAIL_SYSTEM,
                             _email_content(obj, atts.get(obj["message_id"], [])), EMAIL_SCHEMA, effort="medium",
                             max_tokens=6000, timeout=120)
        return scope, obj["message_id"], ans, rec

    items = [("doc", d) for d in doc_sample] + [("email", e) for e in email_sample]
    print(f"reviewing {len(doc_sample)} documents and {len(email_sample)} emails with {REVIEW_MODEL}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for scope, ref, ans, rec in pool.map(work, items):
            save_call(con, rec)
            if ans is None:
                print("  review failed:", ref, rec["error"])
                continue
            con.execute("INSERT OR REPLACE INTO review_results VALUES (?,?,?,?,?,?)",
                        (scope, ref, ans.get("doc_type") or ans.get("email_type"), ans.get("chase_level"),
                         json.dumps(ans), REVIEW_MODEL))
    con.commit()


def rereview(con, scope="email"):
    """Re-run the second opinion on the SAME sample (e.g. after changing the instructions), for a fair before/after."""
    refs = [r[0] for r in con.execute("SELECT ref FROM review_results WHERE scope=?", (scope,))]
    con.execute("DELETE FROM review_results WHERE scope=?", (scope,))
    con.commit()
    emails = {r["message_id"]: r for r in con.execute("SELECT * FROM emails")}
    atts = {}
    for a in con.execute("SELECT a.message_id, a.filename, a.status, d.doc_type, d.vendor_name AS vendor, "
                         "i.invoice_number AS inv_no FROM attachments a LEFT JOIN doc_results d USING(sha256) "
                         "LEFT JOIN invoices i USING(sha256) WHERE a.status != 'missing_inline_image'"):
        atts.setdefault(a["message_id"], []).append(a)

    def work(ref):
        e = emails[ref]
        return ref, *call_json("review_email", ref, REVIEW_MODEL, EMAIL_SYSTEM, _email_content(e, atts.get(ref, [])),
                               EMAIL_SCHEMA, effort="medium", max_tokens=6000, timeout=120)

    print(f"re-reviewing the same {len(refs)} emails with {REVIEW_MODEL}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ref, ans, rec in pool.map(work, refs):
            save_call(con, rec)
            if ans is None:
                print("  review failed:", ref, rec["error"])
                continue
            con.execute("INSERT OR REPLACE INTO review_results VALUES (?,?,?,?,?,?)",
                        ("email", ref, ans["email_type"], ans["chase_level"], json.dumps(ans), REVIEW_MODEL))
    con.commit()


def report(con):
    """Returns a dict; also used by the agent's accuracy tool."""
    out = {}
    # --- 1. rule agreement (everything)
    for scope, table in (("documents", "doc_results"), ("emails", "email_results")):
        r = con.execute(f"SELECT count(*) n, sum(agrees_with_rule IS NOT NULL) judged, sum(agrees_with_rule) agree, "
                        f"sum(unsure) unsure FROM {table} WHERE model IS NOT NULL").fetchone()
        out[f"{scope}_rule_agreement"] = dict(
            classified=r["n"], rules_had_an_opinion=r["judged"], agreed=r["agree"],
            rate=round((r["agree"] or 0) / r["judged"], 3) if r["judged"] else None, marked_unsure=r["unsure"])

    # --- 2. second opinion, re-weighted by class frequency
    for scope, table, key, col in (("documents", "doc_results", "sha256", "doc_type"),
                                   ("emails", "email_results", "message_id", "email_type")):
        rows = con.execute(
            f"SELECT t.{col} AS pred, t.final_confidence AS conf, v.review_type AS rev, "
            + ("t.chase_level AS pchase, v.review_chase AS rchase " if scope == "emails" else "NULL AS pchase, NULL AS rchase ")
            + f"FROM {table} t JOIN review_results v ON v.ref = t.{key} AND v.scope = ?",
            ("doc" if scope == "documents" else "email",)).fetchall()
        if not rows:
            out[f"{scope}_second_opinion"] = "not run yet"
            continue
        freq = dict(con.execute(f"SELECT {col}, count(*) FROM {table} WHERE model IS NOT NULL GROUP BY {col}").fetchall())
        total = sum(freq.values())
        per_class = {}
        for cls in sorted({r["pred"] for r in rows}):
            rs = [r for r in rows if r["pred"] == cls]
            k = sum(r["pred"] == r["rev"] for r in rs)
            per_class[cls] = dict(sampled=len(rs), agreed=k, rate=round(k / len(rs), 3), ci95=wilson(k, len(rs)),
                                  share_of_inbox=round(freq.get(cls, 0) / total, 3))
        weighted = sum(v["rate"] * freq.get(c, 0) for c, v in per_class.items()) / max(
            1, sum(freq.get(c, 0) for c in per_class))
        buckets = {}
        for name, lo, hi in (("high (>=0.9)", 0.9, 1.01), ("medium (0.6-0.9)", 0.6, 0.9), ("low (<0.6)", 0.0, 0.6)):
            rs = [r for r in rows if lo <= (r["conf"] or 0) < hi]
            if rs:
                k = sum(r["pred"] == r["rev"] for r in rs)
                buckets[name] = dict(n=len(rs), agreed_rate=round(k / len(rs), 3))
        entry = dict(review_model=REVIEW_MODEL, sampled=len(rows), weighted_agreement=round(weighted, 3),
                     per_class=per_class, by_confidence=buckets)
        if scope == "emails":
            chase_rows = [r for r in rows if r["rchase"] is not None]
            k = sum(r["pchase"] == r["rchase"] for r in chase_rows)
            entry["chase_level_agreement"] = dict(n=len(chase_rows), agreed=k,
                                                  rate=round(k / len(chase_rows), 3) if chase_rows else None)
        out[f"{scope}_second_opinion"] = entry

    # --- 3. hand labels (gold), if any
    gold = con.execute("SELECT l.scope, l.ref, l.label, l.chase, l.note, coalesce(d.doc_type, r.email_type) AS pred, "
                       "r.chase_level AS pchase FROM labels l "
                       "LEFT JOIN doc_results d ON l.scope='doc' AND d.sha256=l.ref "
                       "LEFT JOIN email_results r ON l.scope='email' AND r.message_id=l.ref").fetchall()
    if gold:
        entry = {}
        rand = [g for g in gold if (g["note"] or "random").startswith("random")]
        if rand:  # unbiased: a plain random sample
            k = sum(g["label"] == g["pred"] for g in rand)
            entry["random_set_type_accuracy"] = dict(n=len(rand), correct=k, rate=round(k / len(rand), 3),
                                                     ci95=wilson(k, len(rand)))
            ch = [g for g in rand if g["scope"] == "email" and g["chase"]]
            if ch:
                kc = sum(g["chase"] == g["pchase"] for g in ch)
                entry["random_set_chase_level_accuracy"] = dict(n=len(ch), correct=kc, rate=round(kc / len(ch), 3),
                                                                ci95=wilson(kc, len(ch)))
        chases = [g for g in gold if (g["note"] or "").startswith("chases")]
        if chases:  # emails the agent called a chase: is it really one, and is the level right?
            real = sum(g["chase"] != "none" for g in chases)
            level = sum(g["chase"] == g["pchase"] for g in chases)
            entry["chase_set"] = dict(n=len(chases), really_a_chase=real, chase_precision=round(real / len(chases), 3),
                                      precision_ci95=wilson(real, len(chases)), level_exact=level,
                                      level_accuracy=round(level / len(chases), 3))
        entry["mistakes"] = [dict(scope=g["scope"], ref=g["ref"], label=g["label"], predicted=g["pred"],
                                  chase=g["chase"], predicted_chase=g["pchase"], note=g["note"]) for g in gold
                             if g["label"] != g["pred"] or (g["chase"] and g["chase"] != g["pchase"])][:15]
        out["hand_labels"] = entry
    else:
        out["hand_labels"] = "none yet"
    out["how_to_read"] = ("Rule agreement is a rough check. The second opinion is a stronger model reading the same "
                          "items independently: agreement is an estimate, not ground truth. Hand labels, when present, "
                          "are ground truth. 'by_confidence' shows whether high-confidence answers really are more "
                          "reliable (calibration).")
    return out


def main():
    con = connect()
    args = sys.argv[1:]
    if args and args[0] == "rereview-emails":
        rereview(con, "email")
    elif args and args[0] == "review":
        per_class = int(args[args.index("--per-class") + 1]) if "--per-class" in args else 16
        review(con, per_class)
    print(json.dumps(report(con), indent=2))


if __name__ == "__main__":
    main()

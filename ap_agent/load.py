"""Step 1: load the PostgreSQL dump into SQLite and link every attachment to its file.

The dump is never executed. It is read as text: only the data blocks
(`COPY ... FROM stdin`) are parsed, so nothing in it can run commands or touch files.

Run: python -m ap_agent.load
"""
import json
import os
import re
import zipfile
from html.parser import HTMLParser
from html import unescape

from .config import ATTACH_DIR, DUMP_PATH, NESTED_DIR
from .db import connect, log_issue

STEP = "load"
NULL = "\\N"
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def copy_unescape(field: str):
    """Decode one field of PostgreSQL's COPY text format."""
    if field == NULL:
        return None
    if "\\" not in field:
        return field
    out, i, n = [], 0, len(field)
    while i < n:
        c = field[i]
        if c == "\\" and i + 1 < n:
            d = field[i + 1]
            if d in _ESC:
                out.append(_ESC[d])
                i += 2
                continue
            if d in "01234567":
                j = i + 1
                while j < n and j < i + 4 and field[j] in "01234567":
                    j += 1
                out.append(chr(int(field[i + 1:j], 8)))
                i = j
                continue
            if d == "x" and i + 2 < n:
                j = i + 2
                while j < n and j < i + 4 and field[j] in "0123456789abcdefABCDEF":
                    j += 1
                out.append(chr(int(field[i + 2:j], 16)))
                i = j
                continue
            out.append(d)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def read_copy_blocks(path):
    """Yield (table, columns, rows) for every COPY ... FROM stdin block."""
    table = cols = None
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if table is None:
                m = re.match(r"COPY (\w+)\.(\w+) \((.*)\) FROM stdin;", line)
                if m:
                    table, cols, rows = m.group(2), m.group(3).split(", "), []
                continue
            if line.startswith("\\."):
                yield table, cols, rows
                table = None
                continue
            rows.append([copy_unescape(x) for x in line.rstrip("\n").split("\t")])


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _HTMLText()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", unescape(html))
    text = "".join(p.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _domain(addr):
    if not addr or "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].lower().strip(">")


def load_emails(con):
    blocks = {t: (c, r) for t, c, r in read_copy_blocks(DUMP_PATH)}
    # Keep the raw tables too, so any question the summary tables can't answer can still be asked in SQL.
    for table, (cols, rows) in blocks.items():
        con.execute(f"DROP TABLE IF EXISTS raw_{table}")
        con.execute(f"CREATE TABLE raw_{table} ({', '.join(c + ' TEXT' for c in cols)})")
        con.executemany(f"INSERT INTO raw_{table} VALUES ({','.join('?' * len(cols))})", rows)

    conv_cols, conv_rows = blocks["frontapp_conversations"]
    conv_subject = {r[conv_cols.index("id")]: r[conv_cols.index("subject")] for r in conv_rows}

    cols, rows = blocks["frontapp_messages"]
    ix = {c: i for i, c in enumerate(cols)}
    con.execute("DELETE FROM emails")
    con.execute("DELETE FROM attachments")
    n_att = 0
    for r in rows:
        g = lambda c: r[ix[c]]
        recipients = json.loads(g("recipients") or "[]")
        to = [x["handle"] for x in recipients if x.get("role") in ("to", "cc")]
        reply_to = [x["handle"] for x in recipients if x.get("role") == "reply-to"]
        text = (g("body_text") or "").strip()
        html_text = html_to_text(g("body_html") or "")
        # Some senders put only a footer in the plain-text part; then the HTML part is the real body.
        if len(text) < 200 and len(html_text) > len(text) * 2:
            body, source = html_text, "html"
        else:
            body, source = text, "text"
        event = g("event_type")
        from_email = (g("recipient_from_handle") or "").lower()
        atts = json.loads(g("attachments") or "[]")
        con.execute(
            "INSERT INTO emails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                g("frontapp_message_id"), g("id"), g("conversation_id"), conv_subject.get(g("conversation_id")),
                (g("message_created_at") or "").replace(" ", "T").replace("+00", "Z"),
                "inbound" if event == "INBOUND" else "outbound",
                event, g("recipient_from_name"), from_email, _domain(from_email),
                ", ".join(to), ", ".join(reply_to), g("subject"), body, source, len(atts),
            ),
        )
        for a in atts:
            n_att += 1
            con.execute(
                "INSERT OR IGNORE INTO attachments(message_id, attachment_id, filename, content_type, size_declared, is_inline)"
                " VALUES (?,?,?,?,?,?)",
                (g("frontapp_message_id"), a["id"], a.get("filename"), a.get("content_type"), a.get("size"),
                 1 if (a.get("metadata") or {}).get("is_inline") else 0),
            )
    print(f"emails: {len(rows)}  attachment references: {n_att}")


def link_files(con):
    """Match attachment references to delivered files; record what is missing, never skip silently."""
    on_disk = {}
    for name in os.listdir(ATTACH_DIR):
        fid = name.split("__", 1)[0]
        on_disk[fid] = ATTACH_DIR / name
    referenced = set()
    rows = con.execute("SELECT message_id, attachment_id, filename, content_type, is_inline FROM attachments").fetchall()
    for r in rows:
        referenced.add(r["attachment_id"])
        path = on_disk.get(r["attachment_id"])
        if path:
            con.execute("UPDATE attachments SET disk_path=?, status='ok' WHERE message_id=? AND attachment_id=?",
                        (str(path), r["message_id"], r["attachment_id"]))
            continue
        inline_image = r["is_inline"] and (r["content_type"] or "").startswith("image/")
        status = "missing_inline_image" if inline_image else "missing"
        detail = ("inline image (logo/signature) not included in attachments.zip" if inline_image
                  else "listed on the email but not included in attachments.zip")
        con.execute("UPDATE attachments SET status=?, detail=? WHERE message_id=? AND attachment_id=?",
                    (status, detail, r["message_id"], r["attachment_id"]))
        if status == "missing":
            log_issue(con, STEP, "attachment", f"{r['message_id']}/{r['attachment_id']}", "file_missing",
                      f"{r['filename']} ({r['content_type']}) is referenced but was not delivered")
    for fid, path in on_disk.items():
        if fid not in referenced:
            log_issue(con, STEP, "attachment", fid, "file_not_referenced",
                      f"{path.name} is in the zip but no email references it")


def expand_zips(con):
    """A zip attached to an email: list it and pull its members out as attachments of the same email."""
    NESTED_DIR.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT * FROM attachments WHERE status='ok' AND lower(filename) LIKE '%.zip'").fetchall()
    for r in rows:
        try:
            with zipfile.ZipFile(r["disk_path"]) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    base = re.split(r"[\\/]", info.filename)[-1]
                    safe = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
                    if info.file_size > 200 * 1024 * 1024:
                        log_issue(con, STEP, "attachment", r["attachment_id"], "zip_member_too_large", base)
                        continue
                    dest = NESTED_DIR / f"{r['attachment_id']}__{safe}"
                    with z.open(info) as src, open(dest, "wb") as out:
                        out.write(src.read())
                    con.execute(
                        "INSERT OR REPLACE INTO attachments VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r["message_id"], f"{r['attachment_id']}#{safe}", safe, None, info.file_size, 0,
                         str(dest), None, "from_zip", f"extracted from {r['filename']}"),
                    )
            print(f"  zip expanded: {r['filename']} (members added as attachments of the same email)")
        except zipfile.BadZipFile as e:
            log_issue(con, STEP, "attachment", r["attachment_id"], "zip_unreadable", str(e))


def main():
    con = connect()
    con.execute("DELETE FROM issues WHERE step=?", (STEP,))
    load_emails(con)
    link_files(con)
    expand_zips(con)
    con.commit()
    for row in con.execute("SELECT status, count(*) n FROM attachments GROUP BY status ORDER BY n DESC"):
        print(f"  attachments {row['status']:22s} {row['n']}")
    for row in con.execute("SELECT direction, count(*) n FROM emails GROUP BY direction"):
        print(f"  emails {row['direction']:10s} {row['n']}")


if __name__ == "__main__":
    main()

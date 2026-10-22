"""Step 2: read every unique file once (duplicates share one result) and record how it was read.

- PDFs with a text layer: text is extracted locally, free.
- Scanned PDFs (no text layer) and images: the first pages are rendered to PNG for the model to look at.
- Spreadsheets, Word files, CSV, text, calendar files: read locally.
- Password-protected or broken files: recorded with the reason, never skipped silently.
Nothing is opened in a desktop application, so no macros or PDF scripts can run.

Run: python -m ap_agent.extract
"""
import email
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from email import policy

import pymupdf
from docx import Document
from openpyxl import load_workbook

from .config import CACHE_DIR, SCAN_PAGES
from .db import connect, log_issue
from .load import html_to_text

STEP = "extract"
PNG_DIR = CACHE_DIR / "pages"
MAX_STORE = 60000  # characters of text kept per file


def sniff(data: bytes, filename: str, content_type: str) -> str:
    head = data[:16]
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head.startswith(b"GIF8"):
        return "image"
    if head.startswith(b"PK\x03\x04"):
        if name.endswith(".xlsx") or "spreadsheet" in ctype:
            return "xlsx"
        if name.endswith(".docx") or "wordprocessing" in ctype:
            return "docx"
        return "zip"
    if "rfc822" in ctype or name.endswith(".eml"):
        return "eml"
    if "calendar" in ctype or "ics" in ctype or name.endswith(".ics"):
        return "ics"
    if "html" in ctype or name.endswith((".html", ".htm")):
        return "html"
    if name.endswith(".csv") or "csv" in ctype:
        return "csv"
    if "json" in ctype or data.lstrip()[:1] in (b"{", b"["):
        return "json"
    if "text" in ctype or name.endswith(".txt"):
        return "text"
    return "unknown"


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"GIF8"):
        return "gif"
    return "jpeg"


MAX_SIDE = 1568  # px on the long side: Claude's recommended image size, and well under the 10 MB limit


def render_pages(doc, sha, n):
    """Render the first n pages as JPEG, scaled so the long side is at most MAX_SIDE pixels."""
    paths = []
    for i in range(n):
        rect = doc[i].rect
        zoom = min(110 / 72, MAX_SIDE / max(rect.width, rect.height))
        pix = doc[i].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        p = PNG_DIR / f"{sha}_p{i + 1}.jpg"
        p.write_bytes(pix.tobytes("jpeg", jpg_quality=85))
        paths.append(str(p))
    return paths


def rerender_unclassified(con):
    """Re-render scans that still have no classification (e.g. their images were too large)."""
    rows = con.execute("SELECT f.sha256, a.disk_path FROM files f JOIN attachments a USING(sha256) "
                       "WHERE f.read_method='vision' AND f.kind='pdf' AND f.sha256 NOT IN (SELECT sha256 FROM doc_results) "
                       "GROUP BY f.sha256").fetchall()
    for r in rows:
        doc = pymupdf.open(r["disk_path"])
        paths = render_pages(doc, r["sha256"], min(doc.page_count, SCAN_PAGES))
        con.execute("UPDATE files SET image_paths=? WHERE sha256=?", (json.dumps(paths), r["sha256"]))
    con.commit()
    print(f"re-rendered {len(rows)} scans as size-capped JPEG")


def read_pdf(sha, data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(""):
        return dict(status="password_protected", pages=None, text="", read_method="none",
                    detail="PDF is password-protected; contents cannot be read")
    pages = doc.page_count
    text = "\n".join(doc[i].get_text() for i in range(pages))
    if len(text.strip()) >= 50:
        return dict(status="ok", pages=pages, text=text[:MAX_STORE], read_method="text", detail=None)
    # No text layer: a scan. Render the first pages for the model to read.
    paths = render_pages(doc, sha, min(pages, SCAN_PAGES))
    detail = f"scanned PDF ({pages} pages), no text layer; first {len(paths)} page(s) sent as images"
    return dict(status="scanned", pages=pages, text="", read_method="vision", detail=detail, image_paths=paths)


def read_xlsx(data):
    import io
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"## sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(cells):
                out.append("\t".join(cells).rstrip())
            if sum(len(x) for x in out) > MAX_STORE:
                break
    return "\n".join(out)


def read_docx(data):
    import io
    d = Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def read_one(sha, sample):
    data = open(sample["disk_path"], "rb").read()
    kind = sniff(data, sample["filename"], sample["content_type"])
    base = dict(sha256=sha, kind=kind, size=len(data), pages=None, image_paths=None)
    try:
        if kind == "pdf":
            res = read_pdf(sha, data)
        elif kind == "image":
            p = PNG_DIR / f"{sha}.{_image_mime(data)}"
            p.write_bytes(data)
            res = dict(status="ok", text="", read_method="vision", detail="image file; sent to the model as an image",
                       image_paths=[str(p)])
        elif kind == "xlsx":
            res = dict(status="ok", text=read_xlsx(data), read_method="text", detail=None)
        elif kind == "docx":
            res = dict(status="ok", text=read_docx(data), read_method="text", detail=None)
        elif kind == "eml":
            msg = email.message_from_bytes(data, policy=policy.default)
            body = msg.get_body(preferencelist=("plain", "html"))
            content = body.get_content() if body else ""
            if body and body.get_content_type() == "text/html":
                content = html_to_text(content)
            res = dict(status="ok", text=f"Subject: {msg['subject']}\nFrom: {msg['from']}\n\n{content}",
                       read_method="text", detail="attached email")
        elif kind == "html":
            res = dict(status="ok", text=html_to_text(data.decode("utf-8", "replace")), read_method="text", detail=None)
        elif kind in ("csv", "text", "ics", "json"):
            res = dict(status="ok", text=data.decode("utf-8", "replace"), read_method="text", detail=None)
        elif kind == "zip":
            res = dict(status="ok", text="", read_method="none", detail="zip archive; its members are read separately")
        else:
            res = dict(status="unsupported", text="", read_method="none", detail="unknown file type")
    except Exception as e:  # recorded, never swallowed
        res = dict(status="unreadable", text="", read_method="none", detail=f"{type(e).__name__}: {str(e)[:200]}")
    base.update(res)
    base["text"] = (base.get("text") or "")[:MAX_STORE]
    if base["status"] == "ok" and base["read_method"] == "text" and not base["text"].strip():
        base["status"], base["detail"] = "empty", "file opened but contains no text"
    return base


def main():
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    if "--rerender-unclassified" in __import__("sys").argv:
        return rerender_unclassified(con)
    con.execute("DELETE FROM issues WHERE step=?", (STEP,))
    rows = con.execute(
        "SELECT message_id, attachment_id, filename, content_type, disk_path FROM attachments "
        "WHERE status IN ('ok','from_zip')").fetchall()
    # Hash every delivered file; identical content is read (and later classified) only once.
    groups = {}
    for r in rows:
        sha = hashlib.sha256(open(r["disk_path"], "rb").read()).hexdigest()
        con.execute("UPDATE attachments SET sha256=? WHERE message_id=? AND attachment_id=?",
                    (sha, r["message_id"], r["attachment_id"]))
        groups.setdefault(sha, []).append(r)
    done = {x[0] for x in con.execute("SELECT sha256 FROM files")}
    todo = [(sha, g[0]) for sha, g in groups.items() if sha not in done]
    print(f"delivered files: {len(rows)}  unique contents: {len(groups)}  to read now: {len(todo)}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: read_one(*t), todo))
    for res in results:
        sha = res["sha256"]
        con.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sha, groups[sha][0]["filename"], len(groups[sha]), res["kind"], res["size"], res.get("pages"),
             res["text"], len(res["text"]), res["read_method"], res["status"], res.get("detail"),
             json.dumps(res.get("image_paths")) if res.get("image_paths") else None),
        )
    # Issues come from the files table, not only from this run, so a resumed run keeps earlier problems visible.
    for f in con.execute("SELECT sha256, sample_name, status, detail FROM files "
                         "WHERE status IN ('password_protected', 'unreadable', 'unsupported', 'empty')").fetchall():
        log_issue(con, STEP, "file", f["sha256"], f["status"], f"{f['sample_name']}: {f['detail']}")
    con.commit()
    for row in con.execute("SELECT kind, status, read_method, count(*) n, sum(copies) c FROM files "
                           "GROUP BY 1,2,3 ORDER BY n DESC"):
        print(f"  {row['kind']:7s} {row['status']:20s} {row['read_method']:7s} unique {row['n']:5d}  copies {row['c']}")


if __name__ == "__main__":
    main()

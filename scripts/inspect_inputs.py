"""Read-only safety inspection of the HQ take-home inputs. Nothing is extracted or executed.

Usage: python -I inspect_inputs.py <zip_path> <sql_path>
"""
import collections
import hashlib
import os
import re
import sys
import zipfile

zip_path, sql_path = sys.argv[1], sys.argv[2]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


print("== SHA-256 (look these up on virustotal.com by hash; never upload the files)")
for p in (sql_path, zip_path):
    print(f"{os.path.basename(p)}  {sha256(p)}")

print("\n== ZIP inventory (read from the central directory, nothing extracted)")
RISKY = {".exe", ".dll", ".scr", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta",
         ".lnk", ".iso", ".img", ".msi", ".jar", ".reg", ".cpl", ".docm", ".xlsm", ".pptm", ".dotm", ".xlam",
         ".html", ".htm", ".svg", ".one", ".chm", ".vhd", ".7z", ".rar", ".zip", ".gz", ".tar"}
with zipfile.ZipFile(zip_path) as z:
    infos = z.infolist()
    files = [i for i in infos if not i.is_dir()]
    total_c = sum(i.compress_size for i in files)
    total_u = sum(i.file_size for i in files)
    print(f"entries: {len(infos)}  files: {len(files)}  compressed: {total_c/1e9:.2f} GB  uncompressed: {total_u/1e9:.2f} GB")

    bad_paths = [i.filename for i in infos if i.filename.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", i.filename)
                 or re.match(r"^[A-Za-z]:", i.filename)]
    print(f"path tricks (absolute or ../): {len(bad_paths)}", bad_paths[:10])

    ratios = sorted(((i.file_size / max(i.compress_size, 1)), i.filename, i.file_size) for i in files)[-5:]
    print("highest compression ratios (zip-bomb check):")
    for r, n, s in ratios:
        print(f"  {r:8.1f}x  {s/1e6:8.2f} MB  {n[:110]}")

    enc = [i.filename for i in files if i.flag_bits & 0x1]
    print(f"password-protected entries: {len(enc)}", enc[:5])

    ext = collections.Counter()
    ext_size = collections.Counter()
    risky, double = [], []
    for i in files:
        base = re.split(r"[\\/]", i.filename)[-1]
        e = os.path.splitext(base)[1].lower() or "(none)"
        ext[e] += 1
        ext_size[e] += i.file_size
        if e in RISKY:
            risky.append(i.filename)
        parts = base.lower().split(".")
        if len(parts) >= 3 and "." + parts[-2] in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".png", ".txt"} \
                and "." + parts[-1] in RISKY | {".exe"}:
            double.append(i.filename)
    print("\nfile types (count, total size):")
    for e, c in ext.most_common():
        print(f"  {e:10s} {c:6d}  {ext_size[e]/1e6:9.1f} MB")
    print(f"\nrisky types: {len(risky)}")
    for n in risky[:40]:
        print("  ", n[:140])
    print(f"double extensions (e.g. .pdf.exe): {len(double)}", double[:10])

    tops = collections.Counter(re.split(r"[\\/]", i.filename)[0] for i in files)
    print(f"\ntop-level folders: {len(tops)}; sample:", list(tops.items())[:5])
    print("sample names:", [i.filename for i in files[:8]])

print("\n== SQL file: dangerous-command scan (text only, nothing executed)")
PATTERNS = {
    "COPY ... PROGRAM (runs shell commands)": r"\bPROGRAM\s+'",
    "psql shell escape \\!": r"^\s*\\!",
    "psql \\copy / \\o / \\i / \\w": r"^\s*\\(copy|o|i|ir|w|g)\b",
    "COPY ... TO file": r"\bCOPY\b[^;]*\bTO\s+'",
    "COPY ... FROM file (not stdin)": r"\bCOPY\b[^;\n]*\bFROM\s+'",
    "large-object file access": r"\blo_(import|export)\b",
    "server file read": r"\bpg_(read_file|read_binary_file|ls_dir|stat_file)\b",
    "untrusted languages": r"\bLANGUAGE\s+(plpython|plperlu|plpythonu|plpython3u|c)\b",
    "CREATE EXTENSION": r"\bCREATE\s+EXTENSION\b",
    "ALTER SYSTEM": r"\bALTER\s+SYSTEM\b",
    "dblink / foreign servers": r"\b(dblink|CREATE\s+SERVER|postgres_fdw|file_fdw)\b",
    "roles / privileges": r"\b(CREATE|ALTER)\s+(ROLE|USER)\b|\bGRANT\b|\bSECURITY\s+DEFINER\b",
    "DROP DATABASE / DROP SCHEMA": r"\bDROP\s+(DATABASE|SCHEMA)\b",
    "functions / triggers / DO blocks": r"\bCREATE\s+(OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE|TRIGGER|RULE)\b|^\s*DO\s",
}
hits = collections.Counter()
examples = collections.defaultdict(list)
stmt_kinds = collections.Counter()
copy_blocks = []
in_copy = False
lines = 0
with open(sql_path, encoding="utf-8", errors="replace") as f:
    for line in f:
        lines += 1
        if in_copy:
            if line.startswith("\\."):
                in_copy = False
            continue  # data rows of COPY ... FROM stdin are data, not commands
        m = re.match(r"\s*([A-Z]+(?:\s+[A-Z]+)?)\b", line)
        if m:
            stmt_kinds[m.group(1)] += 1
        if re.match(r"\s*COPY\b.*\bFROM\s+stdin", line, re.I):
            in_copy = True
            copy_blocks.append(line.strip()[:160])
        for name, pat in PATTERNS.items():
            if re.search(pat, line, re.I):
                hits[name] += 1
                if len(examples[name]) < 3:
                    examples[name].append(line.strip()[:160])
print(f"lines: {lines}")
print("statement starts:", stmt_kinds.most_common(15))
print("COPY FROM stdin blocks:", len(copy_blocks))
for c in copy_blocks[:10]:
    print("  ", c)
print("dangerous-pattern hits (outside data blocks):")
for name in PATTERNS:
    print(f"  {hits[name]:5d}  {name}", examples[name][:2] if hits[name] else "")

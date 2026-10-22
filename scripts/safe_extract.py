"""Extract attachments.zip safely into a flat folder.

- names are reduced to their last path part and stripped of characters Windows can't use
- refuses anything that would land outside the target folder
- per-file and total size caps (the zip was checked for bombs, this is a second line)
- never overwrites: a clash gets a numeric suffix

Usage: python -I safe_extract.py <zip_path> <target_dir>
"""
import os
import re
import sys
import zipfile

zip_path, target = sys.argv[1], os.path.abspath(sys.argv[2])
MAX_FILE = 200 * 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024 * 1024
os.makedirs(target, exist_ok=True)

written = total = skipped = 0
with zipfile.ZipFile(zip_path) as z:
    for info in z.infolist():
        if info.is_dir():
            continue
        name = re.split(r"[\\/]", info.filename)[-1]
        name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .") or f"unnamed_{written}"
        dest = os.path.abspath(os.path.join(target, name))
        if os.path.dirname(dest) != target:
            print("SKIP (path):", info.filename)
            skipped += 1
            continue
        if info.file_size > MAX_FILE or total + info.file_size > MAX_TOTAL:
            print("SKIP (size):", info.filename, info.file_size)
            skipped += 1
            continue
        base, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            dest = f"{base} ({n}){ext}"
            n += 1
        with z.open(info) as src, open(dest, "wb") as out:
            left = MAX_FILE
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                left -= len(chunk)
                if left < 0:
                    raise RuntimeError(f"{info.filename} grew past its declared size")
                out.write(chunk)
        written += 1
        total += info.file_size
print(f"written {written} files, {total/1e9:.2f} GB, skipped {skipped}")

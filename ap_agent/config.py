"""Paths, models and prices. Everything can be overridden with environment variables.

Secrets are read from a .env file next to the repo (../.env) or inside it (./.env);
both are git-ignored. The data never lives inside the repo.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(REPO.parent / ".env")
_load_env_file(REPO / ".env")

DATA_DIR = Path(os.environ.get("AP_DATA_DIR", REPO.parent / "data"))
DUMP_PATH = Path(os.environ.get("AP_DUMP", REPO.parent / "incoming" / "ap_inbox_candidate.sql"))
ATTACH_DIR = Path(os.environ.get("AP_ATTACH_DIR", DATA_DIR / "attachments"))
NESTED_DIR = DATA_DIR / "attachments_from_zips"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = Path(os.environ.get("AP_DB", DATA_DIR / "ap_inbox.db"))

# Bulk reading of 5,705 emails and ~5,700 unique files: the cheapest current model.
BULK_MODEL = os.environ.get("AP_BULK_MODEL", "claude-haiku-5-5")
# Independent second opinion on a random sample, used to estimate accuracy.
REVIEW_MODEL = os.environ.get("AP_REVIEW_MODEL", "claude-sonnet-5-5")

# USD per million tokens: (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

# Below this, an answer is reported as "unsure" instead of being stated as fact.
UNSURE_BELOW = 0.6

# How much of a document the model sees (characters of extracted text, pages rendered for scans).
MAX_DOC_CHARS = 12000
MAX_EMAIL_CHARS = 6000
SCAN_PAGES = 2

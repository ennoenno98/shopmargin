"""Download Matrixify exports and refresh the dashboard's data files.

Matrixify can upload each scheduled export to a cloud destination with a stable
link (S3 / Google Drive / Dropbox / FTP). Set those links as env vars and this
script downloads them, unzips if needed (Matrixify "Zip CSV files"), and writes
normalised CSVs the dashboard reads.

    MATRIXIFY_ORDERS_URL="https://…"   -> data/matrixify_orders.csv
    MATRIXIFY_PRODUCTS_URL="https://…" -> data/matrixify_products.csv

The GitHub Action .github/workflows/refresh_matrixify.yml runs this daily and
commits the refreshed files — no Shopify API token required.
"""
from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests


def _normalize_url(url: str) -> str:
    """Turn a normal Google Drive share link into a direct-download URL.

    Accepts .../file/d/<id>/view, open?id=<id>, or uc?id=<id> and returns
    https://drive.google.com/uc?export=download&id=<id>&confirm=t . Non-Drive
    URLs are returned unchanged.
    """
    if "drive.google.com" not in url:
        return url
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", url) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}&confirm=t"
    return url

DATA = Path(__file__).with_name("data")
# env var -> (destination file, name hint used to pick the right sheet from a zip)
TARGETS = {
    "MATRIXIFY_ORDERS_URL": (DATA / "matrixify_orders.csv.gz", "order"),
    "MATRIXIFY_PRODUCTS_URL": (DATA / "matrixify_products.csv", "product"),
}


def _read_bytes(b: bytes) -> pd.DataFrame:
    if b[:2] == b"PK\x03\x04"[:2] and b[2:4] != b"\x03\x04":  # plain xlsx is also PK; handled below
        pass
    try:
        return pd.read_excel(io.BytesIO(b))
    except Exception:
        return pd.read_csv(io.BytesIO(b), low_memory=False)


def _to_dataframe(content: bytes, hint: str = "") -> pd.DataFrame:
    """Read CSV/XLSX bytes, unzipping a Matrixify zip and picking the member
    whose filename matches ``hint`` (e.g. 'order'/'product') when present.
    Works whether you make two separate exports or one combined zip."""
    if content[:2] == b"PK":  # zip archive (xlsx is also PK, so try zip listing)
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            members = [n for n in zf.namelist() if n.lower().endswith((".csv", ".xlsx"))]
            if members:
                chosen = next((n for n in members if hint and hint in n.lower()), members[0])
                inner = zf.read(chosen)
                return (pd.read_excel(io.BytesIO(inner)) if chosen.lower().endswith(".xlsx")
                        else pd.read_csv(io.BytesIO(inner), low_memory=False))
        except zipfile.BadZipFile:
            pass  # not a zip — fall through (e.g. a bare .xlsx)
    return _read_bytes(content)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    did = 0
    for env_var, (dest, hint) in TARGETS.items():
        url = os.environ.get(env_var)
        if not url:
            print(f"skip {dest.name}: {env_var} not set")
            continue
        print(f"downloading {env_var} -> {dest.name}")
        resp = requests.get(_normalize_url(url), timeout=300, allow_redirects=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "text/html" in ctype or resp.content[:15].lstrip().lower().startswith(b"<!doctype html"):
            print(f"  ERROR: {env_var} returned an HTML page, not a file. Check the link is a "
                  "direct download and the file is shared 'Anyone with the link'.", file=sys.stderr)
            return 1
        df = _to_dataframe(resp.content, hint)
        df.to_csv(dest, index=False)  # .gz extension -> pandas writes gzip
        print(f"  wrote {len(df):,} rows -> {dest.name}")
        did += 1
    if did == 0:
        print("Nothing fetched. Set MATRIXIFY_ORDERS_URL and MATRIXIFY_PRODUCTS_URL.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

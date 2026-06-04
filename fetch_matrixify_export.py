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
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

DATA = Path(__file__).with_name("data")
TARGETS = {
    "MATRIXIFY_ORDERS_URL": DATA / "matrixify_orders.csv.gz",
    "MATRIXIFY_PRODUCTS_URL": DATA / "matrixify_products.csv",
}


def _to_dataframe(content: bytes) -> pd.DataFrame:
    """Read CSV/XLSX bytes, transparently unzipping a Matrixify zip first."""
    if content[:2] == b"PK":  # zip archive
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            member = next((n for n in zf.namelist() if n.lower().endswith((".csv", ".xlsx"))), None)
            if not member:
                raise ValueError("Zip contained no .csv/.xlsx file.")
            inner = zf.read(member)
            if member.lower().endswith(".xlsx"):
                return pd.read_excel(io.BytesIO(inner))
            return pd.read_csv(io.BytesIO(inner))
    # not zipped
    if content[:4] == b"PK\x03\x04" or content[:8].startswith(b"\xd0\xcf"):  # xlsx/xls magic
        return pd.read_excel(io.BytesIO(content))
    try:
        return pd.read_excel(io.BytesIO(content))  # .xlsx without obvious magic
    except Exception:
        return pd.read_csv(io.BytesIO(content))


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    did = 0
    for env_var, dest in TARGETS.items():
        url = os.environ.get(env_var)
        if not url:
            print(f"skip {dest.name}: {env_var} not set")
            continue
        print(f"downloading {env_var} -> {dest.name}")
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        df = _to_dataframe(resp.content)
        df.to_csv(dest, index=False)
        print(f"  wrote {len(df):,} rows")
        did += 1
    if did == 0:
        print("Nothing fetched. Set MATRIXIFY_ORDERS_URL and MATRIXIFY_PRODUCTS_URL.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

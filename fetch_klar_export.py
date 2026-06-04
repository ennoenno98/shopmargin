"""Download the Klar marketing export and refresh data/klar_marketing.csv.

This automates the manual export you've been uploading. It reads a stable
export URL from the environment, downloads it (CSV or XLSX), normalises it to
the schema the dashboard expects, and writes data/klar_marketing.csv.

Usage:
    KLAR_MARKETING_EXPORT_URL="https://..." python fetch_klar_export.py

How to get a stable URL (any one works):
  • Klar → Scheduled Exports → deliver the "Marketing Overview" to a Google
    Sheet, then File → Share → Publish to web → CSV, and use that link.
  • A Klar direct "Download export" link, if it stays valid.
  • Any internal endpoint that returns the same columns.

The GitHub Action in .github/workflows/refresh_klar.yml runs this on a
schedule and commits the refreshed CSV so the dashboard always reads fresh
numbers without a manual upload.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).with_name("data") / "klar_marketing.csv"

# Map possible source header names -> our canonical columns.
COLMAP = {
    "Channel Name": "channel",
    "Calendar Month": "month",
    "Cost": "cost",
    "Revenue KPIs Net Revenue": "channel_net_revenue",
    "Revenue KPIs ROAS": "roas",
    "Revenue KPIs Orders": "orders",
}


def _read_any(content: bytes, content_type: str) -> pd.DataFrame:
    if "csv" in content_type or content[:200].lstrip().startswith(b"Channel"):
        return pd.read_csv(io.BytesIO(content))
    return pd.read_excel(io.BytesIO(content))  # needs openpyxl


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={k: v for k, v in COLMAP.items() if k in df.columns})
    keep = [c for c in ("channel", "month", "cost", "channel_net_revenue", "roas", "orders") if c in df.columns]
    df = df[keep].copy()
    df = df[df["channel"].notna() & (df["channel"].astype(str).str.lower() != "totals")]
    df["month"] = pd.to_datetime(df["month"], errors="coerce").dt.date
    for c in ("cost", "channel_net_revenue", "roas", "orders"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def main() -> int:
    url = os.environ.get("KLAR_MARKETING_EXPORT_URL")
    if not url:
        print("ERROR: set KLAR_MARKETING_EXPORT_URL to the Klar export link.", file=sys.stderr)
        return 2
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    df = normalise(_read_any(resp.content, resp.headers.get("Content-Type", "")))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    print(f"Wrote {len(df)} channel rows -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

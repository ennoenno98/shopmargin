"""Download the Klar marketing export and refresh data/klar_marketing.csv.

Automates the manual export. Reads a stable export URL from the environment,
downloads it (CSV or XLSX, monthly or daily), aggregates to channel x month,
and writes data/klar_marketing.csv (the file the dashboard reads).

Usage:
    KLAR_MARKETING_EXPORT_URL="https://..." python fetch_klar_export.py

How to get a stable URL (any one works):
  • Klar → Scheduled Exports → deliver "Marketing Overview" to a Google Sheet,
    then File → Share → Publish to web → CSV, and use that link.
  • A Klar direct "Download export" link, if it stays valid.

The GitHub Action .github/workflows/refresh_klar.yml runs this on a schedule
and commits the refreshed CSV.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import requests

import marketing  # reuse the tolerant Klar reader (daily/monthly, csv/xlsx)

OUT = Path(__file__).with_name("data") / "klar_marketing.csv"


def main() -> int:
    url = os.environ.get("KLAR_MARKETING_EXPORT_URL")
    if not url:
        print("ERROR: set KLAR_MARKETING_EXPORT_URL to the Klar export link.", file=sys.stderr)
        return 2
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    # Guess an extension so the reader picks CSV vs XLSX correctly.
    ctype = resp.headers.get("Content-Type", "")
    ext = ".xlsx" if ("sheet" in ctype or "excel" in ctype or resp.content[:2] == b"PK") else ".csv"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name

    df = marketing.load_marketing(tmp_path)
    if df.empty:
        print("ERROR: could not parse a marketing table from the download.", file=sys.stderr)
        return 1
    agg = df.groupby(["channel", "month"], as_index=False).agg(
        cost=("cost", "sum"), channel_net_revenue=("channel_net_revenue", "sum"))
    agg["month"] = agg["month"].astype(str) + "-01"
    agg = agg[(agg["cost"].fillna(0) != 0) | (agg["channel_net_revenue"].fillna(0) != 0)]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    agg.to_csv(OUT, index=False)
    print(f"Wrote {len(agg)} channel-month rows -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

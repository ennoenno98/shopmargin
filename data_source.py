"""Data source selection.

Fully Matrixify-driven. If both Matrixify exports are present, the dashboard
reads them; otherwise it falls back to the committed JSON sample so the demo
runs with no setup. Always returns the flattened line-item frame
(margin.flatten_orders schema) plus a mode string for the UI banner.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

import margin
import matrixify_client

SAMPLE_JSON = Path(__file__).with_name("data") / "sample_orders.json"


def load_lineitems(cfg: dict, since: date | None = None, until: date | None = None) -> tuple[pd.DataFrame, str]:
    """Load the full flattened line-item frame. Optionally filter to a window."""
    src = cfg.get("source", {})
    orders_p = Path(src.get("orders_file", ""))
    products_p = Path(src.get("products_file", ""))
    tz = cfg.get("timezone", "Europe/Berlin")

    if orders_p.exists() and products_p.exists():
        df = matrixify_client.load(orders_p, products_p, tz)
        mode = "matrixify"
    elif SAMPLE_JSON.exists():
        with open(SAMPLE_JSON, "r", encoding="utf-8") as fh:
            df = margin.flatten_orders(json.load(fh), tz)
        mode = "sample"
    else:
        return pd.DataFrame(), "empty"

    if not df.empty and since and until:
        df = df[(df["order_date"].dt.date >= since) & (df["order_date"].dt.date <= until)]
    return df, mode

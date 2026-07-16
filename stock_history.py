"""Historic stock levels for the Shopify dashboard.

Pure functions — no Streamlit, no git, no network (everything is passed in or
read from a committed CSV), exactly like ``margin.py``.

The Matrixify Products export is overwritten on each daily refresh, so on its
own it only carries the *current* on-hand. Two things feed a real time series,
both written to ``data/stock_history.csv`` (long format: date, sku, on_hand,
source) by ``build_stock_history.py``:

  * **git**       — every daily products commit is a dated snapshot (≈3 weeks
                    deep today, grows by one row/SKU per day for free).
  * **shopifyql** — a deeper backfill exported from Shopify Analytics' ``inventory``
                    dataset (``ending_inventory_units`` per ``product_variant_sku``,
                    ~10 months daily). Optional; dropped in as a seed CSV.

This module reads that series and derives the dashboard's history features:
days-out-of-stock, recent stock sparkline, and out-of-stock events.
"""
from __future__ import annotations

import pandas as pd

HISTORY_COLS = ["date", "sku", "on_hand", "source"]


# --------------------------------------------------------------------------- #
# Assemble / load
# --------------------------------------------------------------------------- #
def assemble(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate long-format (date, sku, on_hand, source) frames into one
    series. On a (date, sku) collision the *last* frame wins, so callers should
    pass lower-priority sources first (e.g. shopilyql backfill, then git, then
    today's snapshot)."""
    parts = [f for f in frames if f is not None and not f.empty]
    if not parts:
        return pd.DataFrame(columns=HISTORY_COLS)
    out = pd.concat(parts, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["sku"] = out["sku"].astype(str).str.strip()
    out["on_hand"] = pd.to_numeric(out["on_hand"], errors="coerce")
    if "source" not in out.columns:
        out["source"] = ""
    out = out.dropna(subset=["date", "sku"])
    out = out[out["sku"].ne("") & out["sku"].ne("nan")]
    # last write wins per (date, sku)
    out = out.drop_duplicates(subset=["date", "sku"], keep="last")
    return out.sort_values(["sku", "date"]).reset_index(drop=True)[HISTORY_COLS]


def load_stock_history(path) -> pd.DataFrame:
    """Read a committed ``stock_history.csv`` into the canonical typed frame."""
    try:
        raw = pd.read_csv(path)
    except (FileNotFoundError, OSError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=HISTORY_COLS)
    if raw.empty:
        return pd.DataFrame(columns=HISTORY_COLS)
    if "source" not in raw.columns:
        raw["source"] = ""
    return assemble([raw])


def series_for(history: pd.DataFrame, sku: str) -> pd.DataFrame:
    """The (date, on_hand) trajectory for one SKU, for a deep-dive chart."""
    if history is None or history.empty:
        return pd.DataFrame(columns=["date", "on_hand"])
    g = history[history["sku"].astype(str) == str(sku)]
    return (g[["date", "on_hand"]].sort_values("date").reset_index(drop=True)
            if not g.empty else pd.DataFrame(columns=["date", "on_hand"]))


# --------------------------------------------------------------------------- #
# Offline self-test:  python stock_history.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    dates = pd.date_range("2026-06-01", "2026-06-12", freq="D")
    a = [10, 9, 7, 5, 3, 2, 1, 0, 0, 0, 0, 0]
    b = [50, 48, 0, 0, 60, 58, 55, 52, 50, 49, 47, 45]
    frames = []
    for sku, vals in [("A", a), ("B", b)]:
        frames.append(pd.DataFrame({"date": dates, "sku": sku, "on_hand": vals,
                                    "source": "test"}))
    hist = assemble(frames)
    assert len(hist) == 24, len(hist)
    assert series_for(hist, "A")["on_hand"].tolist() == a

    # dedup: a later frame overrides an earlier (date, sku)
    override = pd.DataFrame({"date": [dates[0]], "sku": ["A"], "on_hand": [999],
                             "source": ["new"]})
    merged = assemble([frames[0], override])
    assert merged[(merged["sku"] == "A") & (merged["date"] == dates[0])]["on_hand"].iloc[0] == 999
    print("stock_history self-test OK")

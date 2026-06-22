"""Historic stock levels for the Shopify dashboard.

Pure functions — no Streamlit, no git, no network (everything is passed in or
read from a committed CSV), exactly like ``margin.py`` and ``inventory.py``.

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

import numpy as np
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


def latest_date(history: pd.DataFrame):
    return None if history is None or history.empty else history["date"].max()


# --------------------------------------------------------------------------- #
# Derived metrics
# --------------------------------------------------------------------------- #
def days_out_of_stock(history: pd.DataFrame, ref_date=None) -> pd.DataFrame:
    """Per-SKU length of the current zero-run (consecutive trailing snapshots
    with on_hand ≤ 0), plus the date it started.

    Returns columns ``days_oos`` (int — daily snapshots ≈ calendar days) and
    ``oos_since`` (Timestamp / NaT) indexed by sku. A SKU in stock → 0 / NaT.
    Only the run ending at the SKU's latest snapshot counts.
    """
    if history is None or history.empty:
        return pd.DataFrame(columns=["days_oos", "oos_since"])
    rows = {}
    for sku, g in history.sort_values("date").groupby("sku", sort=False):
        vals = pd.to_numeric(g["on_hand"], errors="coerce").to_numpy()
        dates = g["date"].to_numpy()
        n, since = 0, pd.NaT
        for v, d in zip(vals[::-1], dates[::-1]):
            if not np.isnan(v) and v <= 0:
                n += 1
                since = d
            else:
                break
        rows[sku] = (n, pd.Timestamp(since) if since is not pd.NaT else pd.NaT)
    out = pd.DataFrame.from_dict(rows, orient="index", columns=["days_oos", "oos_since"])
    out.index.name = "sku"
    out["days_oos"] = out["days_oos"].astype(int)
    return out


def sparklines(history: pd.DataFrame, last_n: int = 30) -> pd.Series:
    """Per-SKU list of the last ``last_n`` on-hand values (chronological) — for
    an in-cell trend column (st.column_config.LineChartColumn)."""
    if history is None or history.empty:
        return pd.Series(dtype=object, name="spark")
    out = {}
    for sku, g in history.sort_values("date").groupby("sku", sort=False):
        out[sku] = pd.to_numeric(g["on_hand"], errors="coerce").tolist()[-int(last_n):]
    return pd.Series(out, name="spark")


def oos_events(history: pd.DataFrame, lookback: int = 7) -> pd.DataFrame:
    """Detect each transition from in-stock (>0) to out (≤0).

    Columns: sku, went_oos_on, last_in_stock_on, qty_before, depletion_per_day
    (units/day over the ≤``lookback`` snapshots before the crossing). Newest
    events first.
    """
    if history is None or history.empty:
        return pd.DataFrame(columns=["sku", "went_oos_on", "last_in_stock_on",
                                     "qty_before", "depletion_per_day"])
    rows = []
    for sku, g in history.sort_values("date").groupby("sku", sort=False):
        g = g.reset_index(drop=True)
        q = pd.to_numeric(g["on_hand"], errors="coerce").to_numpy()
        d = g["date"].to_list()
        for i in range(1, len(g)):
            if np.isnan(q[i]) or np.isnan(q[i - 1]):
                continue
            if q[i] <= 0 < q[i - 1]:
                j = max(0, i - int(lookback))
                span = max((d[i] - d[j]).days, 1)
                rate = max(float(q[j]) / span, 0.0)
                rows.append({
                    "sku": sku, "went_oos_on": d[i], "last_in_stock_on": d[i - 1],
                    "qty_before": float(q[i - 1]), "depletion_per_day": round(rate, 2),
                })
    ev = pd.DataFrame(rows, columns=["sku", "went_oos_on", "last_in_stock_on",
                                     "qty_before", "depletion_per_day"])
    return ev.sort_values("went_oos_on", ascending=False).reset_index(drop=True)


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
    # A: depletes 10→0 on the 8th day, stays out (current run = 4 days)
    a = [10, 9, 7, 5, 3, 2, 1, 0, 0, 0, 0, 0]
    # B: healthy then dips to 0 once (recovers) — a past event, not current
    b = [50, 48, 0, 0, 60, 58, 55, 52, 50, 49, 47, 45]
    frames = []
    for sku, vals in [("A", a), ("B", b)]:
        frames.append(pd.DataFrame({"date": dates, "sku": sku, "on_hand": vals,
                                    "source": "test"}))
    hist = assemble(frames)
    assert len(hist) == 24, len(hist)

    dos = days_out_of_stock(hist)
    assert dos.loc["A", "days_oos"] == 5, dos.loc["A", "days_oos"]   # 06-08..06-12
    assert dos.loc["A", "oos_since"] == pd.Timestamp("2026-06-08")
    assert dos.loc["B", "days_oos"] == 0                              # in stock now
    assert pd.isna(dos.loc["B", "oos_since"])

    ev = oos_events(hist, lookback=7)
    assert set(ev["sku"]) == {"A", "B"}, ev["sku"].tolist()
    a_ev = ev[ev["sku"] == "A"].iloc[0]
    assert a_ev["went_oos_on"] == pd.Timestamp("2026-06-08")
    assert a_ev["last_in_stock_on"] == pd.Timestamp("2026-06-07")

    sp = sparklines(hist, last_n=8)
    assert sp["A"] == [3, 2, 1, 0, 0, 0, 0, 0], sp["A"]
    assert series_for(hist, "A")["on_hand"].tolist() == a

    # dedup: a later frame overrides an earlier (date, sku)
    override = pd.DataFrame({"date": [dates[0]], "sku": ["A"], "on_hand": [999],
                             "source": ["new"]})
    merged = assemble([frames[0], override])
    assert merged[(merged["sku"] == "A") & (merged["date"] == dates[0])]["on_hand"].iloc[0] == 999
    print("stock_history self-test OK")

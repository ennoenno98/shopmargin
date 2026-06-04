"""Marketing spend ingestion for CM3.

Reads a Klar "Marketing Overview" export (monthly ``Calendar Month`` or daily
``Date``; CSV or XLSX) and exposes it at the grain the engine needs:

    spend_table(df, grain) -> key | cost | net_revenue
        grain="day"   -> key = daily Timestamp
        grain="month" -> key = Period[M]

CM3 allocation (margin.py) spreads each period's marketing cost across that
period's products in proportion to net revenue, using Klar's store-wide net
revenue for the period as the denominator. Daily grain avoids over-allocating a
full month's spend onto a partial end-month.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _find(df: pd.DataFrame, *candidates: str) -> str | None:
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        hit = norm_map.get(_norm(cand))
        if hit:
            return hit
    return None


def load_marketing(path: Path | str) -> pd.DataFrame:
    """Load a Klar marketing export. Returns: channel, date (datetime64), cost,
    channel_net_revenue. ``date`` keeps day precision (month exports land on the
    first of the month)."""
    p = Path(path)
    cols = ["channel", "date", "cost", "channel_net_revenue"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    raw = pd.read_excel(p) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p)

    c_chan = _find(raw, "channel", "Channel Name", "Channel")
    c_when = _find(raw, "date", "Date", "month", "Calendar Month")
    c_cost = _find(raw, "cost", "Cost")
    c_net = _find(raw, "channel_net_revenue", "Revenue KPIs Net Revenue", "Net Revenue")
    if not c_when:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame({
        "channel": raw[c_chan].astype(str) if c_chan else "All",
        "date": pd.to_datetime(raw[c_when], errors="coerce"),
        "cost": pd.to_numeric(raw[c_cost], errors="coerce") if c_cost else 0.0,
        "channel_net_revenue": pd.to_numeric(raw[c_net], errors="coerce") if c_net else pd.NA,
    })
    df = df[df["channel"].str.strip().str.lower() != "totals"]
    return df.dropna(subset=["date"])


def spend_table(df: pd.DataFrame, grain: str = "day") -> pd.DataFrame:
    """Aggregate to one row per period: key, cost, net_revenue."""
    if df.empty:
        return pd.DataFrame(columns=["key", "cost", "net_revenue"])
    key = df["date"].dt.normalize() if grain == "day" else df["date"].dt.to_period("M")
    out = (df.assign(key=key).groupby("key", as_index=False)
           .agg(cost=("cost", "sum"), net_revenue=("channel_net_revenue", "sum")))
    return out


def channel_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Per-channel spend (summed across dates) for display."""
    if df.empty:
        return df
    out = df.groupby("channel", as_index=False).agg(
        cost=("cost", "sum"), net_revenue=("channel_net_revenue", "sum"))
    return out[out["cost"].fillna(0) > 0].sort_values("cost", ascending=False)

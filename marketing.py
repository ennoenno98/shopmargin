"""Marketing spend ingestion for CM3.

Reads a Klar "Marketing Overview" export and reduces it to what the margin
engine needs:

    month (Period 'M') -> {cost, net_revenue}

The export comes in two shapes — monthly (``Calendar Month`` column) or daily
(``Date`` column). Both are handled: dates are bucketed to month and summed.
CM3 allocation (margin.py) then spreads each month's total marketing cost
across products in proportion to net revenue, using the FULL month's net
revenue (from Klar) as the denominator.
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
    """Load a Klar marketing export (CSV or XLSX), monthly or daily.

    Returns long-form columns: channel, month (Period[M]), cost, channel_net_revenue.
    """
    p = Path(path)
    cols = ["channel", "month", "cost", "channel_net_revenue"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    raw = pd.read_excel(p) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p)

    c_chan = _find(raw, "channel", "Channel Name", "Channel")
    c_when = _find(raw, "month", "Calendar Month", "Date")
    c_cost = _find(raw, "cost", "Cost")
    c_net = _find(raw, "channel_net_revenue", "Revenue KPIs Net Revenue", "Net Revenue")
    if not c_when:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame({
        "channel": raw[c_chan].astype(str) if c_chan else "All",
        "month": pd.to_datetime(raw[c_when], errors="coerce").dt.to_period("M"),
        "cost": pd.to_numeric(raw[c_cost], errors="coerce") if c_cost else 0.0,
        "channel_net_revenue": pd.to_numeric(raw[c_net], errors="coerce") if c_net else pd.NA,
    })
    # drop Klar's "Totals" rows (would double-count) and undated rows
    df = df[df["channel"].str.strip().str.lower() != "totals"]
    df = df.dropna(subset=["month"])
    return df


def monthly_marketing(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate to one row per month: cost, net_revenue (store-wide, the
    allocation denominator). Works for both daily and monthly inputs."""
    if df.empty:
        return pd.DataFrame(columns=["month", "cost", "net_revenue"])
    return df.groupby("month", as_index=False).agg(
        cost=("cost", "sum"),
        net_revenue=("channel_net_revenue", "sum"),
    )


def channel_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Per-channel spend (summed across the file's dates) for display."""
    if df.empty:
        return df
    out = df.groupby("channel", as_index=False).agg(
        cost=("cost", "sum"), net_revenue=("channel_net_revenue", "sum"))
    out = out[out["cost"].fillna(0) > 0]
    return out.sort_values("cost", ascending=False)

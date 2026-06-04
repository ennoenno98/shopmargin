"""Marketing spend ingestion for CM3.

Reads the Klar "Marketing Overview" export (channel x month, with cost and
channel net revenue) and reduces it to what the margin engine needs:

    month (Period 'M') -> {cost, net_revenue}

CM3 allocation (in margin.py) then spreads each month's total marketing cost
across products in proportion to each product's net revenue, using the FULL
month's net revenue (from Klar) as the denominator. That makes the allocation
correct even when the dashboard's date window covers only part of a month.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_marketing(path: Path | str) -> pd.DataFrame:
    """Load the Klar marketing CSV. Returns empty frame if the file is absent.

    Expected columns: channel, month, cost, channel_net_revenue (others ignored).
    """
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["channel", "month", "cost", "channel_net_revenue"])
    df = pd.read_csv(p)
    df["month"] = pd.to_datetime(df["month"], errors="coerce").dt.to_period("M")
    for col in ("cost", "channel_net_revenue"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def monthly_marketing(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the channel-level marketing frame to one row per month.

    Returns columns: month (Period[M]), cost, net_revenue.
    ``net_revenue`` is the store-wide net revenue Klar reports for that month
    (sum across channels) — used as the allocation denominator.
    """
    if df.empty:
        return pd.DataFrame(columns=["month", "cost", "net_revenue"])
    grp = df.groupby("month", as_index=False).agg(
        cost=("cost", "sum"),
        net_revenue=("channel_net_revenue", "sum"),
    )
    return grp


def channel_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """Per-channel spend (with ROAS where available) for display."""
    if df.empty:
        return df
    out = df.copy()
    out = out[out["cost"].fillna(0) > 0] if "cost" in out.columns else out
    return out.sort_values("cost", ascending=False)

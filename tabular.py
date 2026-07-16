"""Small shared helpers for the tabular readers/engines.

Tolerant column matching (Matrixify/Klar/ShopifyQL column names vary by
case/space/punctuation), numeric coercion, file reading, and the
UTC→shop-local→calendar-month conversion. Kept in one place so ``margin.py``,
``matrixify_client.py``, ``marketing.py`` and ``build_stock_history.py`` share a
single implementation instead of each carrying its own copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


def norm(s) -> str:
    """Normalise a column name: lowercase, strip everything but a-z0-9."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def pick(df: pd.DataFrame, *candidates: str) -> str | None:
    """Return the real column whose normalised name matches any candidate."""
    norm_map = {norm(c): c for c in df.columns}
    for cand in candidates:
        hit = norm_map.get(norm(cand))
        if hit:
            return hit
    return None


def to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def read_table(path: Path | str) -> pd.DataFrame:
    """Read a CSV or Excel export into a DataFrame."""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p)
    return pd.read_csv(p, low_memory=False)


def to_local_month(order_date, tz: str):
    """Parse ``order_date`` as UTC, convert to shop-local ``tz`` (falling back to
    UTC if the tz database is unavailable), drop the tz, and return
    ``(naive_local, month_period)`` — the calendar-month bucket used everywhere."""
    parsed = pd.to_datetime(order_date, errors="coerce", utc=True)
    try:
        local = parsed.dt.tz_convert(tz)
    except Exception:
        local = parsed
    naive = local.dt.tz_localize(None)
    return naive, naive.dt.to_period("M")


if __name__ == "__main__":
    df = pd.DataFrame(columns=["Variant SKU", "Cost per item", "Line: Tax Total"])
    assert norm("Variant SKU") == "variantsku"
    assert pick(df, "SKU", "Variant SKU") == "Variant SKU"
    assert pick(df, "nope") is None
    assert to_num(pd.Series(["1", "x", "3"])).tolist()[0] == 1.0
    naive, month = to_local_month(pd.Series(["2026-01-31T23:30:00Z"]), "Europe/Berlin")
    assert str(month.iloc[0]) == "2026-02", month.iloc[0]   # 00:30 Berlin = Feb
    print("tabular self-test OK")

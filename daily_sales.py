"""Daily sales totals as an alternative order source for the OOS dashboard.

A store normally feeds the dashboard with a Matrixify *Orders* export (one row
per order line). Where that export isn't available yet, Shopify Analytics daily
totals per SKU do the job for OOS purposes:

    daily sales file   date, sku, units, net_sales        (ShopifyQL ``sales``:
                       net_items_sold / net_sales by product_variant_sku and day)
    SKU meta file      sku, title, unit_cost              (unit_cost = Shopify
                       cost_of_goods_sold / net_items_sold; blank if unknown)

:func:`load_daily_sales` returns the same flattened frame as
``margin.flatten_orders`` — one pseudo order line per (SKU, day) — so
``margin.compute_costs`` and ``oos_impact`` work unchanged. Because there are no
real orders behind the rows, per-order costs can't be applied: the caller should
cost logistics per unit, and payment fees fall back to the default rate on net
sales. The resulting margin is an estimate, not the order-level figure.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

FLAT_COLS = ["order_name", "order_date", "gateway", "order_total", "sku", "product_id",
             "title", "gross_qty", "gross_revenue", "refunded_qty", "refunded_revenue",
             "unit_cost", "weight_g", "country", "month"]


def load_meta(path: Path | str | None) -> pd.DataFrame:
    """SKU meta indexed by sku: title, unit_cost (NaN where unknown)."""
    empty = pd.DataFrame(columns=["title", "unit_cost"]).rename_axis("sku")
    if not path or not Path(path).is_file():
        return empty
    m = pd.read_csv(path, dtype={"sku": str, "title": str})
    m["sku"] = m["sku"].str.strip()
    m["unit_cost"] = pd.to_numeric(m.get("unit_cost"), errors="coerce")
    m["title"] = m.get("title", "").fillna("")
    return m.drop_duplicates("sku").set_index("sku")[["title", "unit_cost"]]


def _absorb_negatives(values: pd.Series) -> pd.Series:
    """Net a negative day (returns exceeding sales) against the days before it.

    The cost engine floors each line at zero, so a negative day would otherwise
    just vanish and inflate the total. Walking backwards and carrying the
    deficit keeps the SKU's total intact (a deficit left at the very start is
    dropped — there is nothing earlier to net it against).
    """
    out, carry = [], 0.0
    for v in reversed(values.tolist()):
        v = float(v) + carry
        carry = min(v, 0.0)
        out.append(max(v, 0.0))
    return pd.Series(list(reversed(out)), index=values.index)


def load_daily_sales(sales_path: Path | str, meta_path: Path | str | None = None) -> pd.DataFrame:
    """Daily (sku, day) totals -> flattened line-item frame (see module docstring)."""
    if not sales_path or not Path(sales_path).is_file():
        return pd.DataFrame(columns=FLAT_COLS)
    s = pd.read_csv(sales_path, dtype={"sku": str})
    s["date"] = pd.to_datetime(s["date"], errors="coerce")
    s["sku"] = s["sku"].str.strip()
    s = s.dropna(subset=["date"]).sort_values(["sku", "date"]).reset_index(drop=True)
    for col in ("units", "net_sales"):
        s[col] = pd.to_numeric(s[col], errors="coerce").fillna(0.0)
        s[col] = s.groupby("sku", group_keys=False)[col].apply(_absorb_negatives)

    meta = load_meta(meta_path)
    return pd.DataFrame({
        "order_name": s["date"].dt.strftime("%Y-%m-%d") + "|" + s["sku"],
        "order_date": s["date"],
        "gateway": "",
        "order_total": s["net_sales"],
        "sku": s["sku"],
        "product_id": "",
        "title": s["sku"].map(meta["title"]).fillna(""),
        "gross_qty": s["units"],
        "gross_revenue": s["net_sales"],
        "refunded_qty": 0.0,
        "refunded_revenue": 0.0,
        "unit_cost": s["sku"].map(meta["unit_cost"]),
        "weight_g": 0.0,
        "country": "Unknown",
        "month": s["date"].dt.to_period("M"),
    })[FLAT_COLS]


if __name__ == "__main__":
    import tempfile

    a = _absorb_negatives(pd.Series([5.0, 3.0, -2.0, 4.0, -10.0]))
    assert a.tolist() == [0.0, 0.0, 0.0, 0.0, 0.0] and a.sum() == 0          # 5+3-2+4-10 = 0
    b = _absorb_negatives(pd.Series([5.0, 3.0, -2.0, 4.0]))
    assert b.tolist() == [5.0, 1.0, 0.0, 4.0] and b.sum() == 10.0            # total kept
    assert _absorb_negatives(pd.Series([-3.0, 2.0])).tolist() == [0.0, 2.0]  # leading deficit dropped

    with tempfile.TemporaryDirectory() as d:
        sp, mp = Path(d) / "s.csv", Path(d) / "m.csv"
        sp.write_text("date,sku,units,net_sales\n2026-10-01,A,10,100\n2026-10-02,A,-2,-20\n"
                      "2026-10-01,B,4,40\n")
        mp.write_text("sku,title,unit_cost\nA,Alpha,2.5\nB,Beta,\n")
        f = load_daily_sales(sp, mp)
        assert list(f.columns) == FLAT_COLS and len(f) == 3
        assert f.loc[f["sku"] == "A", "gross_qty"].tolist() == [8.0, 0.0]    # return netted back
        assert f.loc[f["sku"] == "A", "gross_revenue"].sum() == 80.0
        assert f.loc[f["sku"] == "A", "unit_cost"].iloc[0] == 2.5
        assert f.loc[f["sku"] == "B", "unit_cost"].isna().all()              # unknown cost stays NaN
        assert f["order_name"].is_unique
        assert load_daily_sales(Path(d) / "nope.csv").empty
    print("daily_sales self-test OK")

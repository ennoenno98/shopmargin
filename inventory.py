"""Out-of-stock / low-stock monitoring for the Shopify dashboard.

Pure functions — no Streamlit, no I/O (everything is passed in), exactly like
``margin.py``. This is the Shopify analogue of the Amazon "Margin Analytics"
dashboard's FBA *Days of Supply* monitoring: it combines the Matrixify Products
**inventory snapshot** (on-hand qty, inventory policy, tracker, product status)
with **sales velocity** derived from the Orders line items to flag SKUs that are
out of stock or about to run out — i.e. the inverse of the overstock view.

Definitions
-----------
* ``velocity``       = net units sold per day over a trailing window (refund-netted)
* ``days_of_supply`` = on-hand ÷ velocity   (blank when there were no recent sales)
* status:
    - ``Out of stock``      tracked, on-hand ≤ 0, policy = *deny*  (sales blocked)
    - ``OOS · backorder``   on-hand ≤ 0 but policy = *continue*    (still sellable)
    - ``Low stock``         on-hand > 0 and days_of_supply < threshold
    - ``Not tracked``       Shopify is not tracking this variant's inventory
    - ``OK``                otherwise
* ``lost_sales_per_day`` = velocity × avg unit price, but only for the truly
  out-of-stock sellers (status *Out of stock* with recent demand).
* ``revenue_at_risk``    = lost_sales_per_day × restock lead time.

Inventory in Shopify is a single global pool, so velocity is intentionally
computed across **all countries** — there is no per-market stock.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Status labels (module-level so the UI can reference them without magic strings)
OOS = "Out of stock"
OOS_BACKORDER = "OOS · backorder"
LOW = "Low stock"
OK = "OK"
NOT_TRACKED = "Not tracked"

_RANK = {OOS: 0, OOS_BACKORDER: 1, LOW: 2, OK: 3, NOT_TRACKED: 4}

_VEL_COLS = ["win_units", "win_revenue", "velocity", "avg_price"]


def _net(frame: pd.DataFrame, qty_col: str, refund_col: str) -> pd.Series:
    """Refund-netted, non-negative series for a quantity/revenue column pair."""
    base = pd.to_numeric(frame[qty_col], errors="coerce").fillna(0.0)
    if refund_col in frame.columns:
        refunded = pd.to_numeric(frame[refund_col], errors="coerce").fillna(0.0)
    else:
        refunded = 0.0
    return (base - refunded).clip(lower=0)


def sales_velocity(lineitems: pd.DataFrame, window_days: int = 30,
                   ref_date=None) -> pd.DataFrame:
    """Per-SKU units/day over the trailing ``window_days`` (net of refunds).

    Returns a frame indexed by ``sku`` with ``win_units`` (units sold in the
    window), ``win_revenue``, ``velocity`` (= win_units / window_days) and
    ``avg_price`` (= win_revenue / win_units). Country is ignored on purpose:
    Shopify stock is one global pool.
    """
    if lineitems is None or lineitems.empty or "sku" not in lineitems.columns:
        return pd.DataFrame(columns=_VEL_COLS)
    d = lineitems.dropna(subset=["order_date"])
    if d.empty:
        return pd.DataFrame(columns=_VEL_COLS)
    ref = pd.Timestamp(ref_date) if ref_date is not None else d["order_date"].max()
    start = ref - pd.Timedelta(days=int(window_days))
    w = d[(d["order_date"] > start) & (d["order_date"] <= ref)]
    if w.empty:
        return pd.DataFrame(columns=_VEL_COLS)

    tmp = pd.DataFrame({
        "sku": w["sku"].astype(str).str.strip().values,
        "_q": _net(w, "gross_qty", "refunded_qty").values,
        "_r": _net(w, "gross_revenue", "refunded_revenue").values,
    })
    g = tmp.groupby("sku").agg(win_units=("_q", "sum"), win_revenue=("_r", "sum"))
    g["velocity"] = g["win_units"] / float(window_days)
    g["avg_price"] = g["win_revenue"] / g["win_units"].where(g["win_units"] > 0)
    return g


def build_oos_view(inventory: pd.DataFrame, lineitems: pd.DataFrame, *,
                   window_days: int = 30, low_stock_days: int = 21,
                   restock_lead_days: int = 30, ref_date=None) -> pd.DataFrame:
    """Join the stock snapshot with sales velocity and classify each SKU.

    ``inventory`` is ``matrixify_client.load_inventory`` output (indexed by sku).
    Returns one row per SKU with velocity, days_of_supply, status and lost-sales
    risk, sorted most-urgent-first (OOS by lost sales, then Low by days of
    supply). ``sku`` is returned as a column.
    """
    if inventory is None or inventory.empty:
        return pd.DataFrame()

    out = inventory.copy()
    for col, default in [("on_hand", np.nan), ("inv_policy", ""),
                         ("inv_tracked", False), ("product_status", ""),
                         ("title", ""), ("price", np.nan)]:
        if col not in out.columns:
            out[col] = default

    out = out.join(sales_velocity(lineitems, window_days, ref_date), how="left")
    for col in ("win_units", "win_revenue", "velocity"):
        out[col] = pd.to_numeric(out.get(col), errors="coerce").fillna(0.0)
    if "avg_price" not in out.columns:
        out["avg_price"] = np.nan

    on_hand = pd.to_numeric(out["on_hand"], errors="coerce")
    out["on_hand"] = on_hand
    out["unit_price"] = out["avg_price"].where(
        out["avg_price"].notna(), pd.to_numeric(out["price"], errors="coerce"))

    vel_pos = out["velocity"] > 0
    out["days_of_supply"] = (on_hand.clip(lower=0) / out["velocity"].where(vel_pos))

    tracked = out["inv_tracked"].fillna(False).astype(bool)
    deny = out["inv_policy"].astype(str).str.strip().str.lower().eq("deny")
    empty = on_hand.fillna(0) <= 0
    low = (~empty) & vel_pos & (out["days_of_supply"] < float(low_stock_days))

    out["status"] = np.select(
        [~tracked, empty & deny, empty & ~deny, low],
        [NOT_TRACKED, OOS, OOS_BACKORDER, LOW],
        default=OK,
    )

    losing = (out["status"] == OOS) & vel_pos
    out["lost_sales_per_day"] = np.where(
        losing, out["velocity"] * out["unit_price"].fillna(0.0), 0.0)
    out["revenue_at_risk"] = out["lost_sales_per_day"] * float(restock_lead_days)

    out["_rank"] = out["status"].map(_RANK).fillna(3).astype(int)
    out = out.sort_values(
        ["_rank", "lost_sales_per_day", "days_of_supply"],
        ascending=[True, False, True], na_position="last",
    ).drop(columns="_rank")
    return out.reset_index()


# --------------------------------------------------------------------------- #
# Offline self-test:  python inventory.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    inv = pd.DataFrame({
        "sku": ["A", "B", "C", "D", "E"],
        "on_hand": [0, 5, 1000, -3, 50],
        "inv_policy": ["deny", "deny", "deny", "continue", "deny"],
        "inv_tracked": [True, True, True, True, False],
        "product_status": ["Active"] * 5,
        "title": ["Sold-out hero", "Almost gone", "Plenty", "Backorder ok", "Untracked"],
        "price": [10.0, 20.0, 30.0, 40.0, 50.0],
    }).set_index("sku")

    ref = pd.Timestamp("2026-06-01")
    days = pd.date_range(ref - pd.Timedelta(days=20), ref, freq="D")
    rows = []
    for day in days:
        rows.append({"sku": "A", "order_date": day, "gross_qty": 2, "refunded_qty": 0,
                     "gross_revenue": 20.0, "refunded_revenue": 0.0})
        rows.append({"sku": "B", "order_date": day, "gross_qty": 2, "refunded_qty": 0,
                     "gross_revenue": 40.0, "refunded_revenue": 0.0})
        rows.append({"sku": "C", "order_date": day, "gross_qty": 1, "refunded_qty": 0,
                     "gross_revenue": 30.0, "refunded_revenue": 0.0})
    lineitems = pd.DataFrame(rows)

    view = build_oos_view(inv, lineitems, window_days=30, low_stock_days=14,
                          restock_lead_days=30).set_index("sku")
    print(view[["status", "on_hand", "velocity", "days_of_supply",
                "lost_sales_per_day", "revenue_at_risk"]].to_string())

    assert view.loc["A", "status"] == OOS, view.loc["A", "status"]
    assert view.loc["B", "status"] == LOW, view.loc["B", "status"]
    assert view.loc["C", "status"] == OK, view.loc["C", "status"]
    assert view.loc["D", "status"] == OOS_BACKORDER, view.loc["D", "status"]
    assert view.loc["E", "status"] == NOT_TRACKED, view.loc["E", "status"]
    # A: avg price 10, lost = velocity*10, risk = lost*30; D/E carry no demand.
    a = view.loc["A"]
    assert abs(a["lost_sales_per_day"] - a["velocity"] * 10.0) < 1e-9
    assert abs(a["revenue_at_risk"] - a["lost_sales_per_day"] * 30.0) < 1e-9
    assert view.loc["D", "lost_sales_per_day"] == 0.0
    # most-urgent-first: A (real OOS with demand) sorts above D/E.
    assert build_oos_view(inv, lineitems, window_days=30)["sku"].iloc[0] == "A"
    print("\nself-test OK")

"""Out-of-stock *impact* engine — estimated lost revenue & lost CM3 per SKU.

Pure functions (no Streamlit). The Shopify analogue of the Amazon
"OOS Impact Analytics" dashboard: it combines the daily stock history (when each
SKU was out of stock) with realised sales/margin (the demand it *would* have met)
to estimate what being out of stock cost.

Model, per (SKU, time-bucket):
    stock_days   = history days with on_hand > 0
    oos_days     = history days with on_hand <= 0
    demand_rate  = units sold / stock_days        (units/day while sellable;
                   falls back to the SKU's all-history rate when a bucket has
                   no in-stock days)
    lost_units   = demand_rate * oos_days
    lost_revenue = lost_units * avg selling price
    lost_cm3     = lost_units * CM3 per unit
    oos_rate     = lost_units / (units + lost_units)   (share of demand lost)

Stock is a single global pool (no country split), matching the Shopify margin
model. ``min_demand`` keeps only SKUs whose all-history demand_rate clears a
floor, so noise from near-dead SKUs is excluded.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PER_SKU_BUCKET_COLS = [
    "sku", "bucket", "bucket_start", "stock_days", "oos_days", "units",
    "revenue", "cm3", "demand_rate", "price", "cm3_per_unit",
    "lost_units", "lost_revenue", "lost_cm3",
]


def bucketize(dates: pd.Series, gran: str):
    """Return (period_start, label) for Month or Quarter buckets."""
    d = pd.to_datetime(dates, errors="coerce")
    if gran == "Quarter":
        per = d.dt.to_period("Q")
        return per.dt.start_time, per.astype(str)
    per = d.dt.to_period("M")
    return per.dt.start_time, per.dt.start_time.dt.strftime("%b %Y")


def daily_sales(costed: pd.DataFrame) -> pd.DataFrame:
    """Per (sku, day) realised units / revenue / cm3 from a costed line-item frame."""
    if costed is None or costed.empty:
        return pd.DataFrame(columns=["sku", "day", "units", "revenue", "cm3"])
    d = pd.DataFrame({
        "sku": costed["sku"].astype(str),
        "day": pd.to_datetime(costed["order_date"], errors="coerce").dt.normalize(),
        "units": pd.to_numeric(costed.get("net_qty"), errors="coerce").fillna(0.0),
        "revenue": pd.to_numeric(costed.get("net_revenue"), errors="coerce").fillna(0.0),
        "cm3": pd.to_numeric(costed.get("cm3"), errors="coerce").fillna(0.0),
    }).dropna(subset=["day"])
    return d.groupby(["sku", "day"], as_index=False).agg(
        units=("units", "sum"), revenue=("revenue", "sum"), cm3=("cm3", "sum"))


def compute_impact(history: pd.DataFrame, costed: pd.DataFrame, *, gran: str = "Month",
                   min_demand: float = 0.0) -> pd.DataFrame:
    """Per (SKU, bucket) lost-sales frame. See module docstring for the model."""
    if history is None or history.empty:
        return pd.DataFrame(columns=PER_SKU_BUCKET_COLS)

    h = history.copy()
    h["day"] = pd.to_datetime(h["date"], errors="coerce").dt.normalize()
    h["oos"] = pd.to_numeric(h["on_hand"], errors="coerce") <= 0
    h["bucket_start"], h["bucket"] = bucketize(h["day"], gran)
    sb = h.groupby(["sku", "bucket", "bucket_start"], as_index=False).agg(
        oos_days=("oos", "sum"), days=("oos", "size"))
    sb["stock_days"] = sb["days"] - sb["oos_days"]

    sales = daily_sales(costed)
    if not sales.empty:
        sales["bucket_start"], sales["bucket"] = bucketize(sales["day"], gran)
        salesb = sales.groupby(["sku", "bucket"], as_index=False).agg(
            units=("units", "sum"), revenue=("revenue", "sum"), cm3=("cm3", "sum"))
    else:
        salesb = pd.DataFrame(columns=["sku", "bucket", "units", "revenue", "cm3"])

    m = sb.merge(salesb, on=["sku", "bucket"], how="left")
    for c in ("units", "revenue", "cm3"):
        m[c] = pd.to_numeric(m.get(c), errors="coerce").fillna(0.0)

    # Per-SKU all-history fallbacks (rate / price / cm3-per-unit).
    tot = m.groupby("sku", as_index=False).agg(
        u=("units", "sum"), sd=("stock_days", "sum"), rev=("revenue", "sum"), c=("cm3", "sum"))
    tot["rate_overall"] = tot["u"] / tot["sd"].where(tot["sd"] > 0)
    tot["price_o"] = tot["rev"] / tot["u"].where(tot["u"] > 0)
    tot["cm3u_o"] = tot["c"] / tot["u"].where(tot["u"] > 0)
    m = m.merge(tot[["sku", "rate_overall", "price_o", "cm3u_o"]], on="sku", how="left")

    rate = m["units"] / m["stock_days"].where(m["stock_days"] > 0)
    m["demand_rate"] = rate.where(m["stock_days"] > 0, m["rate_overall"]).fillna(0.0)
    m["price"] = (m["revenue"] / m["units"].where(m["units"] > 0)).fillna(m["price_o"])
    m["cm3_per_unit"] = (m["cm3"] / m["units"].where(m["units"] > 0)).fillna(m["cm3u_o"])

    m["lost_units"] = (m["demand_rate"] * m["oos_days"]).clip(lower=0)
    m["lost_revenue"] = (m["lost_units"] * m["price"]).fillna(0.0).clip(lower=0)
    m["lost_cm3"] = (m["lost_units"] * m["cm3_per_unit"]).fillna(0.0)

    keep = set(tot.loc[tot["rate_overall"].fillna(0) >= float(min_demand), "sku"])
    m = m[m["sku"].isin(keep)]
    return m[PER_SKU_BUCKET_COLS].sort_values(["bucket_start", "sku"]).reset_index(drop=True)


def by_bucket(impact: pd.DataFrame) -> pd.DataFrame:
    """Totals per time bucket: lost revenue/CM3 + OOS rate (share of demand lost)."""
    if impact is None or impact.empty:
        return pd.DataFrame(columns=["bucket", "bucket_start", "lost_revenue",
                                     "lost_cm3", "units", "lost_units", "oos_rate"])
    g = impact.groupby(["bucket", "bucket_start"], as_index=False).agg(
        lost_revenue=("lost_revenue", "sum"), lost_cm3=("lost_cm3", "sum"),
        units=("units", "sum"), lost_units=("lost_units", "sum"))
    g["oos_rate"] = g["lost_units"] / (g["units"] + g["lost_units"]).where(
        (g["units"] + g["lost_units"]) > 0) * 100
    return g.sort_values("bucket_start").reset_index(drop=True)


def by_sku(impact: pd.DataFrame) -> pd.DataFrame:
    """Totals per SKU: lost revenue/CM3 + OOS days, most-affected first."""
    if impact is None or impact.empty:
        return pd.DataFrame(columns=["sku", "lost_revenue", "lost_cm3", "oos_days",
                                     "lost_units", "demand_rate"])
    g = impact.groupby("sku", as_index=False).agg(
        lost_revenue=("lost_revenue", "sum"), lost_cm3=("lost_cm3", "sum"),
        oos_days=("oos_days", "sum"), lost_units=("lost_units", "sum"),
        units=("units", "sum"), stock_days=("stock_days", "sum"))
    g["demand_rate"] = g["units"] / g["stock_days"].where(g["stock_days"] > 0)
    return g.sort_values("lost_cm3", ascending=False).reset_index(drop=True)


def totals(impact: pd.DataFrame) -> dict:
    """Headline KPIs."""
    if impact is None or impact.empty:
        return {"skus_affected": 0, "lost_revenue": 0.0, "lost_cm3": 0.0, "oos_rate": 0.0}
    units = impact["units"].sum()
    lost_units = impact["lost_units"].sum()
    denom = units + lost_units
    return {
        "skus_affected": int((impact.groupby("sku")["lost_units"].sum() > 0).sum()),
        "lost_revenue": float(impact["lost_revenue"].sum()),
        "lost_cm3": float(impact["lost_cm3"].sum()),
        "oos_rate": float(lost_units / denom * 100) if denom > 0 else 0.0,
    }


def stockout_events(history: pd.DataFrame, impact: pd.DataFrame | None = None) -> pd.DataFrame:
    """Contiguous out-of-stock runs per SKU with estimated lost units/revenue/CM3.

    Uses each SKU's all-history demand_rate / price / cm3-per-unit (from
    ``impact``) to value the run. Newest first.
    """
    if history is None or history.empty:
        return pd.DataFrame(columns=["sku", "start", "end", "days", "lost_units",
                                     "lost_revenue", "lost_cm3"])
    # per-SKU rate/price/cm3-per-unit from the impact frame (all-history)
    rate = price = cm3u = {}
    if impact is not None and not impact.empty:
        agg = impact.groupby("sku").agg(u=("units", "sum"), sd=("stock_days", "sum"),
                                        rev=("revenue", "sum"), c=("cm3", "sum"))
        rate = (agg["u"] / agg["sd"].where(agg["sd"] > 0)).fillna(0).to_dict()
        price = (agg["rev"] / agg["u"].where(agg["u"] > 0)).to_dict()
        cm3u = (agg["c"] / agg["u"].where(agg["u"] > 0)).to_dict()

    rows = []
    for sku, g in history.sort_values("date").groupby("sku", sort=False):
        g = g.reset_index(drop=True)
        q = pd.to_numeric(g["on_hand"], errors="coerce").to_numpy()
        d = pd.to_datetime(g["date"]).to_list()
        i = 0
        n = len(g)
        while i < n:
            if not np.isnan(q[i]) and q[i] <= 0:
                j = i
                while j + 1 < n and (not np.isnan(q[j + 1])) and q[j + 1] <= 0:
                    j += 1
                days = (d[j] - d[i]).days + 1
                r = float(rate.get(sku, 0.0) or 0.0)
                lu = r * days
                rows.append({
                    "sku": sku, "start": d[i], "end": d[j], "days": days,
                    "lost_units": lu,
                    "lost_revenue": lu * float(price.get(sku, np.nan) or 0.0),
                    "lost_cm3": lu * float(cm3u.get(sku, np.nan) or 0.0),
                })
                i = j + 1
            else:
                i += 1
    ev = pd.DataFrame(rows, columns=["sku", "start", "end", "days", "lost_units",
                                     "lost_revenue", "lost_cm3"])
    return ev.sort_values("end", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Offline self-test:  python oos_impact.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    days = pd.date_range("2026-03-01", "2026-05-31", freq="D")
    # SKU A: in stock & selling 2/day, then out for all of May (31 days).
    on_hand, rows = 200, []
    for dt in days:
        oos_may = dt.month == 5
        rows.append({"date": dt, "sku": "A", "on_hand": 0 if oos_may else 100, "source": "t"})
    hist = pd.DataFrame(rows)
    # sales: A sells 2/day at €10, cm3 €4/unit — only while in stock (Mar+Apr).
    srows = []
    for dt in days:
        if dt.month != 5:
            srows.append({"order_name": f"o{dt}", "order_date": dt, "sku": "A",
                          "net_qty": 2, "net_revenue": 20.0, "cm3": 8.0})
    costed = pd.DataFrame(srows)

    imp = compute_impact(hist, costed, gran="Month", min_demand=0.5)
    may = imp[imp["bucket"] == "May 2026"].iloc[0]
    assert may["oos_days"] == 31, may["oos_days"]
    assert abs(may["demand_rate"] - 2.0) < 1e-6, may["demand_rate"]      # fallback rate
    assert abs(may["lost_units"] - 62.0) < 1e-6, may["lost_units"]       # 2*31
    assert abs(may["lost_revenue"] - 620.0) < 1e-6, may["lost_revenue"]  # 62*€10
    assert abs(may["lost_cm3"] - 248.0) < 1e-6, may["lost_cm3"]          # 62*€4

    t = totals(imp)
    assert t["skus_affected"] == 1 and abs(t["lost_cm3"] - 248.0) < 1e-6, t
    bb = by_bucket(imp)
    assert set(bb["bucket"]) >= {"Mar 2026", "Apr 2026", "May 2026"}
    ev = stockout_events(hist, imp)
    assert len(ev) == 1 and ev.iloc[0]["days"] == 31, ev
    assert abs(ev.iloc[0]["lost_cm3"] - 248.0) < 1e-6, ev.iloc[0]["lost_cm3"]
    assert by_sku(imp).iloc[0]["sku"] == "A"
    print("oos_impact self-test OK")
    print(bb.to_string(index=False))

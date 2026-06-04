"""Contribution-margin engine for Shopify orders.

Pure functions — no Streamlit, no I/O beyond what's passed in. This keeps the
CM definitions auditable and testable in isolation, exactly like the Amazon
"Margin Analytics" workbook it's modelled on.

CM ladder (consistent with the Amazon file's GP1/GP2/Channel-margin):
    CM1 = Net revenue - COGS
    CM2 = CM1 - logistics/3PL - payment fees - packaging
    CM3 = CM2 - allocated marketing / ad spend

Convention carried over from the Amazon file: aggregate the absolute EUR
columns, then derive each CM% from the totals (Sum CM / Sum revenue x 100) —
never average percentages.
"""
from __future__ import annotations

import pandas as pd

# ---------------------------------------------------------------------------
# 1. Flatten raw Shopify order nodes into a line-item-level frame
# ---------------------------------------------------------------------------

def _money(node: dict | None) -> float:
    """Pull a float out of a Shopify MoneySet ({shopMoney:{amount}})."""
    if not node:
        return 0.0
    try:
        return float(node["shopMoney"]["amount"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _weight_grams(measurement: dict | None) -> float:
    if not measurement:
        return 0.0
    w = (measurement or {}).get("weight") or {}
    val = w.get("value") or 0.0
    unit = (w.get("unit") or "GRAMS").upper()
    try:
        val = float(val)
    except (TypeError, ValueError):
        return 0.0
    return val * 1000.0 if unit == "KILOGRAMS" else val


def flatten_orders(orders: list[dict], tz: str = "Europe/Berlin") -> pd.DataFrame:
    """Turn a list of Shopify order nodes into one row per (order, line item).

    Refunds are matched back to their line by SKU within the same order.
    ``tz`` is the shop timezone — order dates are converted to it before the
    calendar-month bucket is derived, so months line up with Klar's reporting
    (which uses shop-local calendar months).
    """
    rows: list[dict] = []
    for o in orders:
        gateways = o.get("paymentGatewayNames") or []
        gateway = gateways[0] if gateways else "unknown"
        order_total = _money(o.get("totalPriceSet"))
        created = o.get("createdAt")

        # Refunded quantity / revenue per SKU within this order.
        refunded_qty: dict[str, float] = {}
        refunded_rev: dict[str, float] = {}
        for ref in o.get("refunds") or []:
            for rli in (ref.get("refundLineItems") or {}).get("nodes", []):
                sku = ((rli.get("lineItem") or {}).get("sku")) or ""
                refunded_qty[sku] = refunded_qty.get(sku, 0.0) + (rli.get("quantity") or 0)
                refunded_rev[sku] = refunded_rev.get(sku, 0.0) + _money(rli.get("subtotalSet"))

        for li in (o.get("lineItems") or {}).get("nodes", []):
            sku = li.get("sku") or ""
            qty = li.get("quantity") or 0
            gross_rev = _money(li.get("discountedTotalSet"))
            inv = (li.get("variant") or {}).get("inventoryItem") or {}
            unit_cost_node = inv.get("unitCost")
            unit_cost = float(unit_cost_node["amount"]) if unit_cost_node else None
            r_qty = refunded_qty.get(sku, 0.0)
            r_rev = refunded_rev.get(sku, 0.0)
            rows.append({
                "order_name": o.get("name"),
                "order_date": created,
                "gateway": gateway,
                "order_total": order_total,
                "sku": sku,
                "product_id": (li.get("product") or {}).get("id"),
                "title": li.get("title"),
                "gross_qty": qty,
                "gross_revenue": gross_rev,
                "refunded_qty": r_qty,
                "refunded_revenue": r_rev,
                "unit_cost": unit_cost,
                "weight_g": _weight_grams(inv.get("measurement")),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "country" not in df.columns:
        df["country"] = "Unknown"
    parsed = pd.to_datetime(df["order_date"], errors="coerce", utc=True)
    try:
        local = parsed.dt.tz_convert(tz)
    except Exception:
        local = parsed  # fall back to UTC if tz database is unavailable
    df["order_date"] = local.dt.tz_localize(None)
    df["month"] = df["order_date"].dt.to_period("M")
    return df


# ---------------------------------------------------------------------------
# 2. Apply the cost model to compute per-line CM1 / CM2 / CM3
# ---------------------------------------------------------------------------

def compute_costs(df: pd.DataFrame, cfg: dict, spend: pd.DataFrame | None = None) -> pd.DataFrame:
    """Add cost and CM columns to the flattened line-item frame.

    ``spend`` is marketing.spend_table() output: columns key / cost / net_revenue,
    keyed by day (Timestamp) or month (Period[M]) per cfg['marketing']['grain'].
    """
    if df.empty:
        return df
    d = df.copy()

    rev_cfg = cfg.get("revenue", {})
    # --- Net revenue & units (CM1 inputs) ---
    d["net_revenue"] = d["gross_revenue"]
    if rev_cfg.get("net_of_refunds", True):
        d["net_revenue"] = d["net_revenue"] - d["refunded_revenue"]
    d["net_qty"] = d["gross_qty"] - (d["refunded_qty"] if rev_cfg.get("net_of_refunds", True) else 0)
    d["net_revenue"] = d["net_revenue"].clip(lower=0)
    d["net_qty"] = d["net_qty"].clip(lower=0)

    # --- COGS ---
    default_cost = (cfg.get("cogs") or {}).get("default_unit_cost")
    d["cost_missing"] = d["unit_cost"].isna()
    eff_unit_cost = d["unit_cost"]
    if default_cost is not None:
        eff_unit_cost = eff_unit_cost.fillna(float(default_cost))
        d["cost_missing"] = d["unit_cost"].isna() & False  # filled, no longer "missing"
    eff_unit_cost = eff_unit_cost.fillna(0.0)
    d["cogs"] = eff_unit_cost * d["net_qty"]
    d["cm1"] = d["net_revenue"] - d["cogs"]

    # --- Order-level totals for allocation ---
    grp = d.groupby("order_name")
    d["order_units"] = grp["net_qty"].transform("sum")
    d["order_net_revenue"] = grp["net_revenue"].transform("sum")

    def _alloc(series_value, basis_col):
        """Spread an order-level value across its lines by units or revenue."""
        if basis_col == "units":
            share = d["net_qty"] / d["order_units"].where(d["order_units"] > 0)
        else:  # revenue
            share = d["net_revenue"] / d["order_net_revenue"].where(d["order_net_revenue"] > 0)
        return (series_value * share).fillna(0.0)

    # --- Logistics / 3PL ---
    lg = cfg.get("logistics", {})
    basis = lg.get("basis", "per_order")
    if basis == "per_order":
        order_logi = pd.Series(lg.get("per_order", 0.0), index=d.index)
        # one charge per order, split across that order's lines
        d["logistics"] = _alloc(order_logi, lg.get("allocation", "units"))
    elif basis == "per_unit":
        d["logistics"] = lg.get("per_unit", 0.0) * d["net_qty"]
    elif basis == "per_kg":
        d["logistics"] = lg.get("per_kg", 0.0) * (d["weight_g"] * d["net_qty"] / 1000.0)
    elif basis == "pct_revenue":
        d["logistics"] = lg.get("pct_revenue", 0.0) * d["net_revenue"]
    else:
        d["logistics"] = 0.0
    # Avoid charging a per-order/unit cost to fully-refunded lines.
    d.loc[d["net_qty"] <= 0, "logistics"] = 0.0

    # --- Packaging ---
    pk = cfg.get("packaging", {})
    if pk.get("enabled", False):
        if pk.get("per_unit"):
            d["packaging"] = pk.get("per_unit", 0.0) * d["net_qty"]
        else:
            order_pack = pd.Series(pk.get("per_order", 0.0), index=d.index)
            d["packaging"] = _alloc(order_pack, "units")
        d.loc[d["net_qty"] <= 0, "packaging"] = 0.0
    else:
        d["packaging"] = 0.0

    # --- Payment / transaction fees ---
    pf = cfg.get("payment_fees", {})
    rates = pf.get("gateways", {})
    default_rate = pf.get("default_rate", 0.0)
    fee_base_col = "order_total" if pf.get("basis", "order_total") == "order_total" else "order_net_revenue"
    # rate per row from its gateway
    rate = d["gateway"].map(lambda g: rates.get(g, default_rate)).astype(float)
    # one fee per order: rate * base + fixed, computed once then allocated
    order_first = d.drop_duplicates("order_name").set_index("order_name")
    per_order_fee = (
        order_first[fee_base_col] * order_first["gateway"].map(lambda g: rates.get(g, default_rate)).astype(float)
        + pf.get("fixed_per_order", 0.0)
    )
    d["_order_fee"] = d["order_name"].map(per_order_fee)
    d["payment_fee"] = _alloc(d["_order_fee"], pf.get("allocation", "revenue"))
    d = d.drop(columns=["_order_fee"])

    d["cm2"] = d["cm1"] - d["logistics"] - d["packaging"] - d["payment_fee"]

    # --- Marketing / ad spend (CM3) ---
    mk = cfg.get("marketing", {})
    d["marketing"] = 0.0
    d["cm3_pending"] = False
    if mk.get("enabled", True):
        source = mk.get("source", "file")
        if source == "pct_revenue":
            d["marketing"] = mk.get("pct_revenue", 0.0) * d["net_revenue"]
        elif spend is not None and not spend.empty:
            grain = mk.get("grain", "day")
            alloc = mk.get("allocation", "revenue")
            d["_key"] = d["order_date"].dt.normalize() if grain == "day" else d["order_date"].dt.to_period("M")
            sp = spend.set_index("key")
            # denominator: Klar's store-wide net revenue for the period (fallback
            # to this window's own revenue if Klar didn't report it).
            window_rev = d.groupby("_key")["net_revenue"].sum()
            window_units = d.groupby("_key")["net_qty"].sum()
            def _line_mkt(row):
                kk = row["_key"]
                if kk not in sp.index:
                    return 0.0
                cost = sp.loc[kk, "cost"]
                # Denominator = the export's own period revenue/units, so each
                # period's spend sums exactly to that period's cost (self-
                # consistent; avoids basis mismatch with Klar's net-revenue).
                if alloc == "units":
                    denom = window_units.get(kk, 0.0)
                    num = row["net_qty"]
                else:
                    denom = window_rev.get(kk, 0.0)
                    num = row["net_revenue"]
                return float(cost) * (num / denom) if denom and denom > 0 else 0.0
            d["marketing"] = d.apply(_line_mkt, axis=1)
            d = d.drop(columns=["_key"])
        else:
            d["cm3_pending"] = True  # no marketing source available

    d["cm3"] = d["cm2"] - d["marketing"]
    return d


# ---------------------------------------------------------------------------
# 3. Aggregate to per-product (or per-variant) rows
# ---------------------------------------------------------------------------

SUM_COLS = [
    "net_revenue", "gross_revenue", "cogs", "net_qty", "gross_qty",
    "logistics", "packaging", "payment_fee", "marketing",
    "cm1", "cm2", "cm3", "refunded_revenue", "refunded_qty",
]


def aggregate(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Roll the line-item frame up to one row per SKU (or product).

    Sums the absolute EUR/qty columns, then derives CM%s from the totals.
    """
    if df.empty:
        return df
    grain = (cfg.get("scope") or {}).get("grain", "variant")
    key = "product_id" if grain == "product" else "sku"

    agg = {c: "sum" for c in SUM_COLS if c in df.columns}
    agg.update({
        "title": "first",
        "product_id": "first",
        "sku": "first",
        "cost_missing": "max",   # True if any line lacked a unit cost
        "cm3_pending": "max",
    })
    out = df.groupby(key, as_index=False).agg(agg)

    # Derive percentages from totals (Amazon-file convention).
    rev = out["net_revenue"].where(out["net_revenue"] > 0)
    for cm in ("cm1", "cm2", "cm3"):
        out[f"{cm}_pct"] = (out[cm] / rev * 100)

    out["orders"] = df.groupby(key)["order_name"].nunique().reindex(out[key]).values \
        if key in out.columns else 0
    return out.sort_values("net_revenue", ascending=False).reset_index(drop=True)


def summary_totals(df: pd.DataFrame) -> dict:
    """Portfolio totals + blended CM%s for the KPI row."""
    if df.empty:
        return {}
    rev = df["net_revenue"].sum()
    out = {
        "net_revenue": rev,
        "cogs": df["cogs"].sum(),
        "logistics": df["logistics"].sum(),
        "payment_fee": df["payment_fee"].sum(),
        "packaging": df["packaging"].sum(),
        "marketing": df["marketing"].sum(),
        "cm1": df["cm1"].sum(),
        "cm2": df["cm2"].sum(),
        "cm3": df["cm3"].sum(),
        "orders": df["order_name"].nunique() if "order_name" in df.columns else None,
        "units": df["net_qty"].sum() if "net_qty" in df.columns else None,
    }
    for cm in ("cm1", "cm2", "cm3"):
        out[f"{cm}_pct"] = (out[cm] / rev * 100) if rev else float("nan")
    return out

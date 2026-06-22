"""Matrixify export reader.

The dashboard's data source. Reads two Matrixify exports and produces the same
line-item-level frame that margin.flatten_orders() emits, so the CM engine
downstream is unchanged:

  * Products export  -> COGS (Variant Cost) + weight per SKU
  * Orders export    -> per-line revenue, qty, discounts, refunds, gateway

Matrixify column names vary slightly between versions, so columns are matched
tolerantly (case/space/punctuation-insensitive, by candidate names). Both CSV
and XLSX are accepted.

NOTE: Matrixify Orders exports are multi-row per order — order-level fields
appear on the order's first row and are blank on continuation rows (extra line
items / transactions / refunds). We forward-fill those within each order block.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _col(df: pd.DataFrame, *candidates: str) -> str | None:
    """Return the real column whose normalised name matches any candidate."""
    norm_map = {_norm(c): c for c in df.columns}
    for cand in candidates:
        hit = norm_map.get(_norm(cand))
        if hit:
            return hit
    return None


def _read(path: Path | str) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p)
    return pd.read_csv(p, low_memory=False)


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


# ---------------------------------------------------------------------------
# Products -> cost / weight by SKU
# ---------------------------------------------------------------------------

def load_products(path: Path | str) -> pd.DataFrame:
    """Return per-SKU: unit_cost, weight_g, title, product_id (indexed by sku)."""
    df = _read(path)
    sku = _col(df, "Variant SKU", "SKU")
    cost = _col(df, "Variant Cost", "Cost per item", "Cost")
    grams = _col(df, "Variant Grams", "Grams")
    weight = _col(df, "Variant Weight", "Weight")
    wunit = _col(df, "Variant Weight Unit", "Weight Unit")
    title = _col(df, "Title")
    pid = _col(df, "ID", "Product ID")
    if not sku:
        raise ValueError("Products export has no 'Variant SKU' column.")

    out = pd.DataFrame({"sku": df[sku].astype(str).str.strip()})
    out["unit_cost"] = _num(df[cost]) if cost else pd.NA
    # weight in grams: prefer Variant Grams, else convert Variant Weight by unit
    if grams:
        out["weight_g"] = _num(df[grams]).fillna(0.0)
    elif weight:
        w = _num(df[weight]).fillna(0.0)
        if wunit:
            mult = df[wunit].astype(str).str.lower().map(
                {"kg": 1000, "kilograms": 1000, "g": 1, "grams": 1, "lb": 453.592, "oz": 28.3495}
            ).fillna(1.0)
            out["weight_g"] = w * mult
        else:
            out["weight_g"] = w
    else:
        out["weight_g"] = 0.0
    out["title"] = df[title] if title else ""
    out["product_id"] = df[pid].astype(str) if pid else ""
    out = out[out["sku"] != ""].drop_duplicates("sku").set_index("sku")
    return out


# ---------------------------------------------------------------------------
# Products -> stock snapshot by SKU (for the out-of-stock dashboard)
# ---------------------------------------------------------------------------

def load_inventory(path: Path | str) -> pd.DataFrame:
    """Per-SKU stock snapshot from the Matrixify Products export.

    Indexed by sku, columns: on_hand (Variant Inventory Qty), inv_policy
    (deny/continue), inv_tracked (bool — is Shopify tracking this variant),
    product_status (Active/Draft/Archived/...), title, price, unit_cost,
    handle, url, product_id. Consumed by ``inventory.build_oos_view``.
    """
    return inventory_from_frame(_read(path))


def inventory_from_frame(df: pd.DataFrame) -> pd.DataFrame:
    """The body of :func:`load_inventory`, but on an already-read frame.

    Split out so the stock-history backfill can reuse it on a Products CSV
    pulled straight from a past git revision (``git show <rev>:<file>``).
    """
    sku = _col(df, "Variant SKU", "SKU")
    if not sku:
        raise ValueError("Products export has no 'Variant SKU' column.")
    qty = _col(df, "Variant Inventory Qty", "Variant Inventory Quantity", "Inventory Qty")
    policy = _col(df, "Variant Inventory Policy", "Inventory Policy")
    tracker = _col(df, "Variant Inventory Tracker", "Inventory Tracker")
    pstatus = _col(df, "Status", "Product Status")
    title = _col(df, "Title")
    price = _col(df, "Variant Price", "Price")
    cost = _col(df, "Variant Cost", "Cost per item", "Cost")
    handle = _col(df, "Handle")
    url = _col(df, "URL")
    pid = _col(df, "ID", "Product ID")

    # Matrixify blanks product-level fields on a product's continuation
    # (extra-variant) rows; forward-fill them so every variant keeps its
    # parent's descriptive fields. Per-variant fields (qty/policy/price) stay.
    for c in (pstatus, title, handle, url, pid):
        if c:
            df[c] = df[c].ffill()

    out = pd.DataFrame({"sku": df[sku].astype(str).str.strip()})
    out["on_hand"] = _num(df[qty]) if qty else pd.NA
    out["inv_policy"] = (df[policy].astype(str).str.strip().str.lower()
                         if policy else "")
    if tracker:
        trk = df[tracker].astype(str).str.strip().str.lower()
        out["inv_tracked"] = ~trk.isin(["", "nan", "none"])
    else:
        out["inv_tracked"] = False
    out["product_status"] = df[pstatus].astype(str).str.strip() if pstatus else ""
    out["title"] = df[title] if title else ""
    out["price"] = _num(df[price]) if price else pd.NA
    out["unit_cost"] = _num(df[cost]) if cost else pd.NA
    out["handle"] = df[handle].astype(str) if handle else ""
    out["url"] = df[url].astype(str) if url else ""
    out["product_id"] = df[pid].astype(str) if pid else ""

    out = out[~out["sku"].isin(["", "nan"])].drop_duplicates("sku").set_index("sku")
    return out


# ---------------------------------------------------------------------------
# Orders -> flattened line items (same schema as margin.flatten_orders)
# ---------------------------------------------------------------------------

def load_orders(path: Path | str, products: pd.DataFrame, tz: str = "Europe/Berlin") -> pd.DataFrame:
    """Parse a Matrixify Orders export into the flattened line-item frame.

    Real Matrixify layout: each order spans several rows distinguished by
    ``Line: Type`` — ``Line Item``, ``Shipping Line``, ``Discount``,
    ``Transaction``, ``Refund Line``. Order-level fields sit on the ``Top Row``
    and are blank below, so we forward-fill them. Refunds are negative
    ``Refund Line`` rows (negative qty/total) matched to their SKU.
    """
    df = _read(path)

    c_name = _col(df, "Name", "Order Name")
    c_created = _col(df, "Created At", "Processed At", "Created")
    c_total = _col(df, "Price: Total", "Total", "Price: Current Total")
    l_type = _col(df, "Line: Type")
    l_sku = _col(df, "Line: SKU", "Lineitem SKU")
    l_qty = _col(df, "Line: Quantity", "Lineitem quantity")
    l_total = _col(df, "Line: Total", "Lineitem total")
    l_price = _col(df, "Line: Price", "Lineitem price")
    l_disc = _col(df, "Line: Discount")
    l_title = _col(df, "Line: Title", "Line: Name")
    # gateway: prefer an explicit gateway column, else payment method
    t_gateway = _col(df, "Transaction: Gateway")
    t_method = _col(df, "Transaction: Payment Method")
    c_country = _col(df, "Shipping: Country Code", "Shipping: Country",
                     "Send to Country", "Shipping Country", "Billing: Country Code")
    if not c_name or not l_sku:
        raise ValueError("Orders export needs at least 'Name' and 'Line: SKU' columns.")

    order = df[c_name].ffill()
    ltype = df[l_type].astype(str).str.strip() if l_type else pd.Series([""] * len(df))
    sku = df[l_sku].astype(str).str.strip()
    qty = _num(df[l_qty]) if l_qty else pd.Series(0.0, index=df.index)
    if l_total:
        tot = _num(df[l_total])
    else:
        tot = (_num(df[l_price]) if l_price else 0) * qty - (_num(df[l_disc]) if l_disc else 0)

    is_li = ltype.eq("Line Item") if l_type else (sku != "")
    is_rf = ltype.eq("Refund Line") if l_type else pd.Series(False, index=df.index)

    base = pd.DataFrame({"order": order.values, "sku": sku.values,
                         "qty": qty.values, "rev": tot.values})
    li = base[is_li.values & (base["sku"] != "") & (base["sku"] != "nan")] \
        .groupby(["order", "sku"], as_index=False).agg(gross_qty=("qty", "sum"), gross_revenue=("rev", "sum"))
    rf = base[is_rf.values & (base["sku"] != "") & (base["sku"] != "nan")] \
        .groupby(["order", "sku"], as_index=False).agg(refunded_qty=("qty", "sum"), refunded_revenue=("rev", "sum"))
    if not rf.empty:
        rf["refunded_qty"] = -rf["refunded_qty"]      # negative rows -> positive refunded amounts
        rf["refunded_revenue"] = -rf["refunded_revenue"]

    items = li.merge(rf, on=["order", "sku"], how="outer").fillna(
        {"gross_qty": 0, "gross_revenue": 0, "refunded_qty": 0, "refunded_revenue": 0})
    if items.empty:
        return items

    # order-level attributes
    odf = pd.DataFrame({"order": order.values})
    odf["created"] = df[c_created].ffill().values if c_created else None
    odf["order_total"] = (_num(df[c_total]).ffill().values if c_total else 0.0)
    odf["country"] = (df[c_country].ffill().astype(str).values if c_country else "Unknown")
    olevel = odf.groupby("order", as_index=False).first()

    # gateway per order from transaction rows
    gw_col = t_gateway or t_method
    if gw_col:
        gw = pd.DataFrame({"order": order.values, "gw": df[gw_col].astype(str).values})
        gw = gw[gw["gw"].str.strip().ne("") & gw["gw"].str.lower().ne("nan")]
        gwm = gw.groupby("order", as_index=False).first() if not gw.empty else pd.DataFrame(columns=["order", "gw"])
    else:
        gwm = pd.DataFrame(columns=["order", "gw"])

    items = items.merge(olevel, on="order", how="left").merge(gwm, on="order", how="left")
    items["gateway"] = items.get("gw", pd.Series(index=items.index, dtype=object)).map(_norm_gateway)

    # join product cost / weight / title
    prod = products.reindex(items["sku"])
    out = pd.DataFrame({
        "order_name": items["order"],
        "order_date": items["created"],
        "country": items["country"].fillna("Unknown") if "country" in items else "Unknown",
        "gateway": items["gateway"].fillna("unknown"),
        "order_total": items["order_total"].fillna(0.0),
        "sku": items["sku"],
        "product_id": prod["product_id"].values if "product_id" in prod else None,
        "title": prod["title"].values if "title" in prod else items["sku"],
        "gross_qty": items["gross_qty"],
        "gross_revenue": items["gross_revenue"],
        "refunded_qty": items["refunded_qty"],
        "refunded_revenue": items["refunded_revenue"],
        "unit_cost": prod["unit_cost"].values if "unit_cost" in prod else None,
        "weight_g": prod["weight_g"].values if "weight_g" in prod else 0.0,
    })
    # title fallback to SKU where product not found
    out["title"] = out["title"].where(out["title"].notna(), out["sku"])

    parsed = pd.to_datetime(out["order_date"], errors="coerce", utc=True)
    try:
        local = parsed.dt.tz_convert(tz)
    except Exception:
        local = parsed
    out["order_date"] = local.dt.tz_localize(None)
    out["month"] = out["order_date"].dt.to_period("M")
    return out


def _norm_gateway(g) -> str:
    """Map Matrixify gateway / payment-method labels to config payment_fees keys.

    Shopify Payments processes card + local methods (iDEAL, Bancontact, EPS,
    Sofort, Apple/Google Pay) → 'shopify_payments'. Klarna and PayPal are
    separate. Blank/missing → 'unknown' (uses default_rate).
    """
    n = _norm(g)
    if not n or n == "nan" or n == "none":
        return "unknown"
    if "paypal" in n:
        return "paypal"
    if "klarna" in n:
        return "klarna"
    shopify_methods = {"card", "creditcard", "ideal", "bancontact", "eps", "sofort",
                       "giropay", "applepay", "googlepay", "shopify", "shopifypayments"}
    if "shopifypayment" in n or n in shopify_methods:
        return "shopify_payments"
    return n


def load(orders_path: Path | str, products_path: Path | str, tz: str = "Europe/Berlin") -> pd.DataFrame:
    """Convenience: build the flattened line-item frame from both exports."""
    products = load_products(products_path)
    return load_orders(orders_path, products, tz)

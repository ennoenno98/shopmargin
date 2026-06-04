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
    return pd.read_csv(p)


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
# Orders -> flattened line items (same schema as margin.flatten_orders)
# ---------------------------------------------------------------------------

def load_orders(path: Path | str, products: pd.DataFrame, tz: str = "Europe/Berlin") -> pd.DataFrame:
    df = _read(path)

    c_name = _col(df, "Name", "Order Name")
    c_id = _col(df, "ID", "Order ID")
    c_created = _col(df, "Created At", "Processed At", "Created")
    c_fin = _col(df, "Financial Status")
    c_total = _col(df, "Total")
    # line item
    l_sku = _col(df, "Line: SKU", "Lineitem SKU")
    l_qty = _col(df, "Line: Quantity", "Lineitem quantity")
    l_price = _col(df, "Line: Price", "Lineitem price")
    l_disc = _col(df, "Line: Discount", "Lineitem discount")
    l_total = _col(df, "Line: Total", "Lineitem total")
    l_title = _col(df, "Line: Title", "Lineitem name", "Line: Name")
    l_type = _col(df, "Line: Type")
    # transaction / refund
    t_gateway = _col(df, "Transaction: Gateway")
    t_kind = _col(df, "Transaction: Kind")
    r_sku = _col(df, "Refund: Line: SKU", "Refund Line Item: SKU", "Refund: Line Item: SKU")
    r_qty = _col(df, "Refund: Line: Quantity", "Refund Line Item: Quantity", "Refund: Line Item: Quantity")
    r_sub = _col(df, "Refund: Line: Subtotal", "Refund Line Item: Subtotal", "Refund: Line Item: Subtotal")

    if not c_name or not l_sku:
        raise ValueError("Orders export needs at least 'Name' and 'Line: SKU' columns.")

    # Forward-fill order-level identity & fields across continuation rows.
    ffill_cols = [c for c in (c_name, c_id, c_created, c_fin, c_total) if c]
    df[ffill_cols] = df[ffill_cols].replace("", pd.NA).ffill()

    rows: list[dict] = []
    for name, block in df.groupby(c_name, sort=False):
        created = block[c_created].iloc[0] if c_created else None
        order_total = float(_num(pd.Series([block[c_total].iloc[0]])).iloc[0] or 0.0) if c_total else 0.0

        # gateway: first SALE/CAPTURE transaction's gateway, else first non-null
        gateway = "unknown"
        if t_gateway:
            gws = block[t_gateway].dropna()
            gws = gws[gws.astype(str).str.strip() != ""]
            if t_kind:
                sale = block[block[t_kind].astype(str).str.lower().isin(["sale", "capture"])]
                if t_gateway in sale and sale[t_gateway].notna().any():
                    gateway = str(sale[t_gateway].dropna().iloc[0])
                elif len(gws):
                    gateway = str(gws.iloc[0])
            elif len(gws):
                gateway = str(gws.iloc[0])
        gateway = _norm_gateway(gateway)

        # refunds aggregated by SKU within the order
        ref_qty: dict[str, float] = {}
        ref_rev: dict[str, float] = {}
        if r_sku:
            for _, rr in block.iterrows():
                s = str(rr.get(r_sku) or "").strip()
                if not s:
                    continue
                ref_qty[s] = ref_qty.get(s, 0.0) + float(_num(pd.Series([rr.get(r_qty)])).iloc[0] or 0.0) if r_qty else ref_qty.get(s, 0.0)
                ref_rev[s] = ref_rev.get(s, 0.0) + float(_num(pd.Series([rr.get(r_sub)])).iloc[0] or 0.0) if r_sub else ref_rev.get(s, 0.0)

        # line items: rows with a SKU and (no Line:Type or Type == Line Item)
        for _, li in block.iterrows():
            s = str(li.get(l_sku) or "").strip()
            if not s:
                continue
            if l_type and str(li.get(l_type) or "").strip().lower() not in ("", "line item", "lineitem"):
                continue
            qty = float(_num(pd.Series([li.get(l_qty)])).iloc[0] or 0.0) if l_qty else 0.0
            if l_total and pd.notna(li.get(l_total)):
                gross = float(_num(pd.Series([li.get(l_total)])).iloc[0] or 0.0)
            else:
                price = float(_num(pd.Series([li.get(l_price)])).iloc[0] or 0.0) if l_price else 0.0
                disc = float(_num(pd.Series([li.get(l_disc)])).iloc[0] or 0.0) if l_disc else 0.0
                gross = price * qty - disc
            prod = products.loc[s] if s in products.index else None
            rows.append({
                "order_name": name,
                "order_date": created,
                "gateway": gateway,
                "order_total": order_total,
                "sku": s,
                "product_id": (prod["product_id"] if prod is not None else None),
                "title": (li.get(l_title) if l_title and pd.notna(li.get(l_title))
                          else (prod["title"] if prod is not None else s)),
                "gross_qty": qty,
                "gross_revenue": gross,
                "refunded_qty": ref_qty.get(s, 0.0),
                "refunded_revenue": ref_rev.get(s, 0.0),
                "unit_cost": (float(prod["unit_cost"]) if prod is not None and pd.notna(prod["unit_cost"]) else None),
                "weight_g": (float(prod["weight_g"]) if prod is not None else 0.0),
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    parsed = pd.to_datetime(out["order_date"], errors="coerce", utc=True)
    try:
        local = parsed.dt.tz_convert(tz)
    except Exception:
        local = parsed
    out["order_date"] = local.dt.tz_localize(None)
    out["month"] = out["order_date"].dt.to_period("M")
    return out


def _norm_gateway(g: str) -> str:
    """Map Matrixify gateway labels to the keys used in config payment_fees."""
    n = _norm(g)
    if "paypal" in n:
        return "paypal"
    if "shopifypayment" in n or n in ("shopify", "shopifypayments"):
        return "shopify_payments"
    return g or "unknown"


def load(orders_path: Path | str, products_path: Path | str, tz: str = "Europe/Berlin") -> pd.DataFrame:
    """Convenience: build the flattened line-item frame from both exports."""
    products = load_products(products_path)
    return load_orders(orders_path, products, tz)

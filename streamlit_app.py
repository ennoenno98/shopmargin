"""Shopify Product Margin Dashboard — CM1 / CM2 / CM3 per product.

Data comes from Matrixify exports (Orders + Products) — no Shopify API token
needed — with a committed JSON sample as fallback. Cost assumptions come from
config.yaml and can be overridden in the sidebar. Calculation logic lives in
margin.py; this file is presentation only.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

import os

import data_source
import margin
from config import deep_merge, load_config
from marketing import channel_breakdown, load_marketing, monthly_marketing

st.set_page_config(page_title="Shopify Margin Dashboard", page_icon="📊", layout="wide")

BASE_CFG = load_config()


def _file_sig(cfg: dict) -> str:
    """Cache-busting signature from the Matrixify files' modification times."""
    parts = []
    for k in ("orders_file", "products_file"):
        p = (cfg.get("source") or {}).get(k, "")
        parts.append(f"{p}:{os.path.getmtime(p)}" if p and os.path.exists(p) else f"{p}:0")
    return "|".join(parts)


@st.cache_data(show_spinner="Loading Matrixify data…")
def fetch_lineitems(orders_file: str, products_file: str, tz: str,
                    since: date, until: date, sig: str):
    """Cached load of the flattened line items. ``sig`` busts the cache when
    the underlying export files change (or the Refresh button clears it)."""
    cfg = {"source": {"orders_file": orders_file, "products_file": products_file}, "timezone": tz}
    return data_source.load_lineitems(cfg, since, until)


@st.cache_data(show_spinner=False)
def fetch_marketing(path: str):
    return monthly_marketing(load_marketing(path)), load_marketing(path)


def sidebar_controls() -> tuple[dict, date, date, bool]:
    st.sidebar.title("⚙️ Controls")

    days = int((BASE_CFG.get("date") or {}).get("default_days", 90))
    today = date.today()
    default_since = today - timedelta(days=days)
    rng = st.sidebar.date_input(
        "Date range (order date)",
        value=(default_since, today),
        help="Window pulled from Shopify. Cached per range.",
    )
    since, until = (rng if isinstance(rng, tuple) and len(rng) == 2 else (default_since, today))

    if st.sidebar.button("🔄 Refresh data (clear cache)"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.divider()
    ov: dict = {}

    with st.sidebar.expander("Logistics / 3PL (CM2)", expanded=False):
        lg = BASE_CFG["logistics"]
        basis = st.selectbox("Basis", ["per_order", "per_unit", "per_kg", "pct_revenue"],
                             index=["per_order", "per_unit", "per_kg", "pct_revenue"].index(lg["basis"]))
        val = st.number_input(f"Value ({basis})", value=float(lg.get(basis, 0.0)), step=0.1, format="%.4f")
        alloc = st.selectbox("Allocate across line items by", ["units", "revenue"],
                             index=0 if lg.get("allocation", "units") == "units" else 1)
        ov["logistics"] = {"basis": basis, basis: val, "allocation": alloc}

    with st.sidebar.expander("Payment fees (CM2)", expanded=False):
        pf = BASE_CFG["payment_fees"]
        shp = st.number_input("Shopify Payments rate", value=float(pf["gateways"].get("shopify_payments", 0.016)),
                              step=0.001, format="%.4f")
        ppl = st.number_input("PayPal rate", value=float(pf["gateways"].get("paypal", 0.0298)),
                              step=0.001, format="%.4f")
        fixed = st.number_input("Fixed € per order", value=float(pf.get("fixed_per_order", 0.0)), step=0.05)
        ov["payment_fees"] = {"gateways": {"shopify_payments": shp, "paypal": ppl}, "fixed_per_order": fixed}

    with st.sidebar.expander("Packaging (CM2)", expanded=False):
        pk = BASE_CFG["packaging"]
        enabled = st.checkbox("Include packaging", value=bool(pk.get("enabled", False)))
        per_unit = st.number_input("€ per unit", value=float(pk.get("per_unit", 0.0)), step=0.05)
        ov["packaging"] = {"enabled": enabled, "per_unit": per_unit}

    with st.sidebar.expander("Marketing (CM3)", expanded=False):
        mk = BASE_CFG["marketing"]
        source = st.selectbox("Source", ["file", "pct_revenue"],
                              index=0 if mk.get("source", "file") == "file" else 1,
                              help="file = Klar marketing export; pct_revenue = flat TACoS")
        pct = st.number_input("Fallback TACoS (% of net rev)", value=float(mk.get("pct_revenue", 0.10)),
                              step=0.01, format="%.3f")
        alloc_m = st.selectbox("Allocate spend to products by", ["revenue", "units"],
                               index=0 if mk.get("allocation", "revenue") == "revenue" else 1)
        ov["marketing"] = {"source": source, "pct_revenue": pct, "allocation": alloc_m}

    with st.sidebar.expander("Targets & scope", expanded=False):
        tg = BASE_CFG["targets"]
        c1 = st.number_input("Target CM1 %", value=float(tg["cm1_pct"]), step=1.0)
        c2 = st.number_input("Target CM2 %", value=float(tg["cm2_pct"]), step=1.0)
        c3 = st.number_input("Target CM3 %", value=float(tg["cm3_pct"]), step=1.0)
        grain = st.selectbox("Grain", ["variant", "product"],
                             index=0 if (BASE_CFG.get("scope") or {}).get("grain") == "variant" else 1)
        ov["targets"] = {"cm1_pct": c1, "cm2_pct": c2, "cm3_pct": c3}
        ov["scope"] = {"grain": grain}

    cfg = deep_merge(BASE_CFG, ov)
    return cfg, since, until


def kpi_row(tot: dict, cfg: dict):
    t = cfg["targets"]
    c = st.columns(6)
    c[0].metric("Net revenue", f"€{tot['net_revenue']:,.0f}")
    c[1].metric("CM1", f"€{tot['cm1']:,.0f}", f"{tot['cm1_pct']:.1f}% (t {t['cm1_pct']:.0f}%)")
    c[2].metric("CM2", f"€{tot['cm2']:,.0f}", f"{tot['cm2_pct']:.1f}% (t {t['cm2_pct']:.0f}%)")
    c[3].metric("CM3", f"€{tot['cm3']:,.0f}", f"{tot['cm3_pct']:.1f}% (t {t['cm3_pct']:.0f}%)")
    c[4].metric("Orders", f"{tot.get('orders', 0):,}")
    c[5].metric("Units", f"{tot.get('units', 0):,.0f}")


def render_table(agg: pd.DataFrame, cfg: dict):
    t = cfg["targets"]
    show = agg.rename(columns={
        "sku": "SKU", "title": "Product", "net_revenue": "Net Rev €", "net_qty": "Units",
        "cogs": "COGS €", "logistics": "Logistics €", "payment_fee": "Pay fee €",
        "marketing": "Marketing €", "cm1": "CM1 €", "cm2": "CM2 €", "cm3": "CM3 €",
        "cm1_pct": "CM1 %", "cm2_pct": "CM2 %", "cm3_pct": "CM3 %", "orders": "Orders",
    })
    cols = ["SKU", "Product", "Orders", "Units", "Net Rev €", "COGS €",
            "CM1 €", "CM1 %", "Logistics €", "Pay fee €", "CM2 €", "CM2 %",
            "Marketing €", "CM3 €", "CM3 %", "cost_missing"]
    cols = [c for c in cols if c in show.columns]
    view = show[cols]

    def hl(col, target):
        return lambda v: "color:#b00020" if pd.notna(v) and v < target else "color:#1b7a34"

    sty = view.style.format({
        "Net Rev €": "€{:,.0f}", "COGS €": "€{:,.0f}", "CM1 €": "€{:,.0f}",
        "CM2 €": "€{:,.0f}", "CM3 €": "€{:,.0f}", "Logistics €": "€{:,.1f}",
        "Pay fee €": "€{:,.1f}", "Marketing €": "€{:,.1f}",
        "CM1 %": "{:.1f}%", "CM2 %": "{:.1f}%", "CM3 %": "{:.1f}%", "Units": "{:.0f}",
    }, na_rep="—")
    for c, tgt in [("CM1 %", t["cm1_pct"]), ("CM2 %", t["cm2_pct"]), ("CM3 %", t["cm3_pct"])]:
        if c in view.columns:
            sty = sty.map(hl(c, tgt), subset=[c])

    st.dataframe(sty, use_container_width=True, height=520,
                 column_config={"cost_missing": st.column_config.CheckboxColumn("Cost missing?")})
    st.download_button("⬇️ Download CSV", view.to_csv(index=False).encode(),
                       "shopify_margins.csv", "text/csv")


def main():
    cfg, since, until = sidebar_controls()
    st.title("📊 Shopify Product Margin Dashboard")
    st.caption("CM1 = Revenue − COGS · CM2 = − logistics/3PL − payment fees − packaging · "
               "CM3 = − allocated marketing")

    src = cfg["source"]
    df, mode = fetch_lineitems(src["orders_file"], src["products_file"],
                               cfg.get("timezone", "Europe/Berlin"), since, until,
                               _file_sig(cfg))
    if mode == "sample":
        st.warning("**Sample mode** — Matrixify exports not found, showing the committed "
                   "sample. Drop `matrixify_orders.csv` + `matrixify_products.csv` in `data/` "
                   "(or let the scheduled fetch populate them) for live numbers.")
    elif mode == "matrixify":
        st.success("Data source: **Matrixify exports** (Orders + Products).")
    if df.empty:
        st.info("No orders in the selected window.")
        return

    monthly_mkt, mkt_raw = fetch_marketing(cfg["marketing"]["spend_file"])
    d = margin.compute_costs(df, cfg, monthly_mkt)
    agg = margin.aggregate(d, cfg)
    tot = margin.summary_totals(d)

    kpi_row(tot, cfg)
    below = int((agg["cm3_pct"] < cfg["targets"]["cm3_pct"]).sum())
    misses = int(agg["cost_missing"].sum())
    msg = f"⚠️ {below} of {len(agg)} SKUs below CM3 target ({cfg['targets']['cm3_pct']:.0f}%)."
    if misses:
        msg += f"  ·  {misses} SKU(s) missing a Shopify unit cost (COGS treated as €0)."
    if bool(d["cm3_pending"].max()):
        msg += "  ·  Some months have no marketing data → CM3 = CM2 for those."
    st.info(msg)

    tab1, tab2, tab3 = st.tabs(["📋 Per-product margins", "📈 Charts", "📣 Marketing"])

    with tab1:
        render_table(agg, cfg)

    with tab2:
        cc = st.columns(2)
        with cc[0]:
            st.subheader("CM3 % distribution")
            fig = px.histogram(agg, x="cm3_pct", nbins=30)
            fig.add_vline(x=cfg["targets"]["cm3_pct"], line_dash="dot", line_color="#d32f2f")
            fig.update_layout(xaxis_title="CM3 %", yaxis_title="SKUs", height=360)
            st.plotly_chart(fig, use_container_width=True)
        with cc[1]:
            st.subheader("CM ladder (portfolio €)")
            ladder = pd.DataFrame({
                "stage": ["Net rev", "−COGS", "CM1", "−Logi", "−Fees", "−Pkg", "CM2", "−Mktg", "CM3"],
                "value": [tot["net_revenue"], -tot["cogs"], tot["cm1"], -tot["logistics"],
                          -tot["payment_fee"], -tot["packaging"], tot["cm2"], -tot["marketing"], tot["cm3"]],
            })
            wf = px.bar(ladder, x="stage", y="value")
            wf.update_layout(height=360, yaxis_title="€")
            st.plotly_chart(wf, use_container_width=True)

        st.subheader("Top / bottom performers")
        metric = st.radio("Rank by", ["cm3", "cm3_pct", "net_revenue"], horizontal=True,
                          format_func=lambda x: {"cm3": "CM3 €", "cm3_pct": "CM3 %", "net_revenue": "Net rev €"}[x])
        n = st.slider("How many", 5, 20, 10)
        ranked = agg.dropna(subset=[metric]).sort_values(metric, ascending=False)
        gc = st.columns(2)
        with gc[0]:
            top = ranked.head(n)
            st.plotly_chart(px.bar(top, x=metric, y="sku", orientation="h", hover_data=["title"],
                                   title=f"Top {n}").update_layout(height=400, yaxis={"autorange": "reversed"}),
                            use_container_width=True)
        with gc[1]:
            bot = ranked.tail(n)
            st.plotly_chart(px.bar(bot, x=metric, y="sku", orientation="h", hover_data=["title"],
                                   title=f"Bottom {n}").update_layout(height=400, yaxis={"autorange": "reversed"}),
                            use_container_width=True)

    with tab3:
        st.subheader("Marketing spend (Klar export)")
        if mkt_raw.empty:
            st.warning(f"No marketing file at `{cfg['marketing']['spend_file']}`. "
                       "CM3 falls back to the TACoS % if you switch source in the sidebar.")
        else:
            st.caption(f"Total spend in file: €{monthly_mkt['cost'].sum():,.0f} across "
                       f"{len(monthly_mkt)} month(s). Allocated to products pro-rata by "
                       f"{cfg['marketing']['allocation']} (denominator = Klar's full-month net revenue).")
            st.dataframe(channel_breakdown(mkt_raw), use_container_width=True)
            st.plotly_chart(px.bar(channel_breakdown(mkt_raw), x="cost", y="channel", orientation="h",
                                   title="Spend by channel").update_layout(height=380, yaxis={"autorange": "reversed"}),
                            use_container_width=True)


main()

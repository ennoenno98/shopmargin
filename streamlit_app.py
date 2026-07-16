"""Shopify Product Margin Dashboard — CM1 / CM2 / CM3 per product.

Modelled on the Amazon "Margin Analytics" dashboard: source header, filter
card (country · granularity · period · search · top-sellers · min-sales),
five KPIs, a margin×volume cluster grid, a conditionally-formatted per-product
table, and Overview / Margin Trend / Slow movers tabs.

Data comes from Matrixify exports (Orders + Products) — no Shopify API token —
with a committed JSON sample as fallback. Cost assumptions live in config.yaml
and are overridable in the sidebar. Calculation logic lives in margin.py.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

import data_source
import margin
from config import deep_merge, load_config
from marketing import channel_breakdown, load_marketing, spend_table

st.set_page_config(page_title="Shopify Margin Analytics", page_icon="📈", layout="wide")
BASE_CFG = load_config()

GOOD, BAD, MIDC = "#C6EFCE", "#F8CBAD", "#FFF2CC"

CLUSTER_NAMES = {
    "1-1": "High margin · High sales", "1-2": "High margin · Mid sales", "1-3": "High margin · Low sales",
    "2-1": "Mid margin · High sales", "2-2": "Mid margin · Mid sales", "2-3": "Mid margin · Low sales",
    "3-1": "Low margin · High sales", "3-2": "Low margin · Mid sales", "3-3": "Low margin · Low sales",
}


# --------------------------------------------------------------------------- #
# Data loading (cached)
# --------------------------------------------------------------------------- #
def _file_sig(cfg: dict) -> str:
    parts = []
    for k in ("orders_file", "products_file"):
        p = (cfg.get("source") or {}).get(k, "")
        parts.append(f"{p}:{os.path.getmtime(p)}" if p and os.path.exists(p) else f"{p}:0")
    mp = cfg["marketing"]["spend_file"]
    parts.append(f"{mp}:{os.path.getmtime(mp)}" if os.path.exists(mp) else f"{mp}:0")
    return "|".join(parts)


@st.cache_data(show_spinner="Loading data…")
def load_all(orders_file: str, products_file: str, tz: str, spend_file: str, sig: str):
    cfg = {"source": {"orders_file": orders_file, "products_file": products_file}, "timezone": tz}
    df, mode = data_source.load_lineitems(cfg)
    return df, mode, load_marketing(spend_file)


# --------------------------------------------------------------------------- #
# Period bucketing
# --------------------------------------------------------------------------- #
def add_buckets(df: pd.DataFrame, gran: str) -> pd.DataFrame:
    d = df["order_date"]
    if gran == "Day":
        start = d.dt.normalize()
        label = start.dt.strftime("%Y-%m-%d")
    elif gran == "Week":
        start = d.dt.to_period("W-SUN").dt.start_time
        label = start.dt.strftime("KW %V · %G")
    elif gran == "Quarter":
        per = d.dt.to_period("Q")
        start = per.dt.start_time
        label = per.astype(str)
    else:  # Month
        per = d.dt.to_period("M")
        start = per.dt.start_time
        label = start.dt.strftime("%b %Y")
    out = df.copy()
    out["bucket_start"] = start
    out["bucket"] = label
    return out


def ordered_buckets(df: pd.DataFrame) -> list[str]:
    return (df[["bucket", "bucket_start"]].drop_duplicates()
            .sort_values("bucket_start")["bucket"].tolist())


# --------------------------------------------------------------------------- #
# Clusters (margin × volume terciles)
# --------------------------------------------------------------------------- #
def _tier(s: pd.Series) -> pd.Series:
    """1 = top third, 2 = middle, 3 = bottom third."""
    s = pd.to_numeric(s, errors="coerce")
    if s.notna().sum() < 3:
        return pd.Series(2, index=s.index)
    hi, lo = s.quantile(2 / 3), s.quantile(1 / 3)
    return s.apply(lambda v: 1 if v >= hi else (3 if v <= lo else 2)).astype(int)


def add_clusters(agg: pd.DataFrame) -> pd.DataFrame:
    a = agg.copy()
    m, v = _tier(a["cm3_pct"]), _tier(a["net_revenue"])
    a["_m"], a["_v"] = m, v
    a["cluster_code"] = m.astype(str) + "-" + v.astype(str)
    a["Cluster"] = a["cluster_code"].map(CLUSTER_NAMES)
    return a


# --------------------------------------------------------------------------- #
# Sidebar — cost assumptions
# --------------------------------------------------------------------------- #
def sidebar_controls() -> dict:
    st.sidebar.title("⚙️ Assumptions")
    if st.sidebar.button("🔄 Refresh data (clear cache)"):
        st.cache_data.clear()
        st.rerun()
    ov: dict = {}
    with st.sidebar.expander("Logistics / 3PL", expanded=False):
        lg = BASE_CFG["logistics"]
        basis = st.selectbox("Basis", ["per_order", "per_unit", "per_kg", "pct_revenue"],
                             index=["per_order", "per_unit", "per_kg", "pct_revenue"].index(lg["basis"]))
        val = st.number_input(f"Value ({basis})", value=float(lg.get(basis, 0.0)), step=0.1, format="%.4f")
        ov["logistics"] = {"basis": basis, basis: val}
    with st.sidebar.expander("Payment fees", expanded=False):
        pf = BASE_CFG["payment_fees"]
        shp = st.number_input("Shopify Payments", value=float(pf["gateways"]["shopify_payments"]), step=0.001, format="%.4f")
        ppl = st.number_input("PayPal", value=float(pf["gateways"]["paypal"]), step=0.001, format="%.4f")
        kl = st.number_input("Klarna", value=float(pf["gateways"].get("klarna", 0.0299)), step=0.001, format="%.4f")
        ov["payment_fees"] = {"gateways": {"shopify_payments": shp, "paypal": ppl, "klarna": kl}}
    with st.sidebar.expander("Packaging", expanded=False):
        pk = BASE_CFG["packaging"]
        ov["packaging"] = {"enabled": st.checkbox("Include packaging", value=bool(pk.get("enabled", False))),
                           "per_unit": st.number_input("€/unit", value=float(pk.get("per_unit", 0.0)), step=0.05)}
    with st.sidebar.expander("Marketing", expanded=False):
        mk = BASE_CFG["marketing"]
        ov["marketing"] = {
            "source": st.selectbox("Source", ["file", "pct_revenue"], index=0 if mk["source"] == "file" else 1),
            "pct_revenue": st.number_input("Fallback TACoS", value=float(mk["pct_revenue"]), step=0.01, format="%.3f"),
            "allocation": st.selectbox("Allocate by", ["revenue", "units"], index=0)}
    with st.sidebar.expander("Targets", expanded=True):
        tg = BASE_CFG["targets"]
        ov["targets"] = {"cm1_pct": st.number_input("Target CM1 %", value=float(tg["cm1_pct"]), step=1.0),
                         "cm2_pct": st.number_input("Target CM2 %", value=float(tg["cm2_pct"]), step=1.0),
                         "cm3_pct": st.number_input("Target CM3 %", value=float(tg["cm3_pct"]), step=1.0)}
    return deep_merge(BASE_CFG, ov)


# --------------------------------------------------------------------------- #
# Cluster grid
# --------------------------------------------------------------------------- #
def render_cluster_grid(agg: pd.DataFrame):
    st.markdown("**Margin × volume clusters** — top third / bottom third by CM3 % and by sales.")
    counts = agg.groupby(["_m", "_v"]).size().to_dict()
    head = st.columns([1.2, 1, 1, 1])
    for c, t in zip(head[1:], ["High sales", "Mid sales", "Low sales"]):
        c.markdown(f"<div style='text-align:center;font-weight:600'>{t}</div>", unsafe_allow_html=True)
    for m, mlabel in [(1, "High margin"), (2, "Mid margin"), (3, "Low margin")]:
        row = st.columns([1.2, 1, 1, 1])
        row[0].markdown(f"<div style='padding-top:18px;font-weight:600'>{mlabel}</div>", unsafe_allow_html=True)
        for vi, v in enumerate([1, 2, 3], start=1):
            n = counts.get((m, v), 0)
            if m == 1 and v == 1:
                bg, mark = GOOD, " ⭐"
            elif m == 3 and v == 1:
                bg, mark = BAD, " ⚠️"
            elif m == 3 and v == 3:
                bg, mark = MIDC, ""
            else:
                bg, mark = ("#EAF4EA" if m == 1 else "#EEF3FB"), ""
            row[vi].markdown(
                f"<div style='background:{bg};border-radius:8px;padding:18px;text-align:center;"
                f"font-size:1.1rem;font-weight:600'>{n} SKUs{mark}</div>", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Per-product table
# --------------------------------------------------------------------------- #
def render_table(view: pd.DataFrame, cfg: dict, key: str = "tbl"):
    t = cfg["targets"]
    cols = {"sku": "SKU", "title": "Product", "Cluster": "Cluster", "net_qty": "Units",
            "net_revenue": "Sales (€)", "cm1_pct": "CM1 %", "cm2_pct": "CM2 %", "cm3_pct": "CM3 %",
            "delta_cm3": "Δ CM3", "cm3": "P&L (€)", "marketing": "Ad spend (€)"}
    present = [c for c in cols if c in view.columns]
    show = view[present].rename(columns=cols)

    fmt = {"Sales (€)": "€{:,.0f}", "P&L (€)": "€{:,.0f}", "Ad spend (€)": "€{:,.0f}",
           "Units": "{:,.0f}", "CM1 %": "{:.1f}%", "CM2 %": "{:.1f}%", "CM3 %": "{:.1f}%",
           "Δ CM3": "{:+.1f} pp"}
    sty = show.style.format({k: v for k, v in fmt.items() if k in show.columns}, na_rep="—")

    def cm_color(col, tgt):
        return lambda v: f"background-color:{BAD};color:#111" if pd.notna(v) and v < tgt \
            else (f"background-color:{GOOD};color:#111" if pd.notna(v) else "")
    for c, tgt in [("CM1 %", t["cm1_pct"]), ("CM2 %", t["cm2_pct"]), ("CM3 %", t["cm3_pct"])]:
        if c in show.columns:
            sty = sty.map(cm_color(c, tgt), subset=[c])
    if "Δ CM3" in show.columns:
        sty = sty.map(lambda v: "color:#1b7a34" if pd.notna(v) and v > 0 else ("color:#b00020" if pd.notna(v) and v < 0 else ""), subset=["Δ CM3"])
    st.dataframe(sty, use_container_width=True, height=560, key=f"df_{key}")
    st.download_button("⬇️ Download CSV", show.to_csv(index=False).encode(),
                       f"shopify_margins_{key}.csv", "text/csv", key=f"dl_{key}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = sidebar_controls()
    src = cfg["source"]
    df_all, mode, mkt_raw = load_all(
        src["orders_file"], src["products_file"], cfg.get("timezone", "Europe/Berlin"),
        cfg["marketing"]["spend_file"], _file_sig(cfg))
    spend = spend_table(mkt_raw, cfg["marketing"].get("grain", "day"))

    # ---- Header + source line ----
    st.markdown("## 📈 Shopify Margin Analytics")
    if df_all.empty:
        st.info("No data available. Add Matrixify exports to `data/`.")
        return
    src_name = os.path.basename(src["orders_file"]) if mode == "matrixify" else "sample_orders.json"
    try:
        refreshed = datetime.fromtimestamp(os.path.getmtime(src["orders_file"]), tz=timezone.utc) \
            if mode == "matrixify" else None
    except OSError:
        refreshed = None
    refresh_txt = refreshed.strftime("%Y-%m-%d %H:%M UTC") if refreshed else "—"
    st.caption(f"Source: `{src_name}` · last refreshed **{refresh_txt}** · "
               f"{df_all['order_name'].nunique():,} orders · {df_all['sku'].nunique():,} SKUs"
               + ("  ·  ⚠️ sample data" if mode == "sample" else ""))

    df_all = add_buckets(df_all, "Week")  # default; re-bucketed below per granularity

    # ---- Filter card ----
    with st.container(border=True):
        r1 = st.columns([1.4, 1.2, 2.2, 2, 1])
        countries = ["All countries"] + sorted(c for c in df_all["country"].dropna().unique()
                                               if c and c != "Unknown")
        country = r1[0].selectbox("Country (shipping)", countries)
        gran = r1[1].radio("Granularity", ["Day", "Week", "Month", "Quarter"], index=1)
        df_b = add_buckets(df_all, gran)
        buckets = ordered_buckets(df_b)
        sel_periods = r1[2].multiselect(f"{gran}(s)", buckets, default=buckets[-1:] if buckets else [])
        search = r1[3].text_input("SKU or Product contains", "")
        top_only = r1[4].toggle("Top sellers only", value=False)
        r2 = st.columns([2, 5])
        min_sales = r2[0].number_input("Min sales in selection (€)", value=0.0, step=100.0, min_value=0.0)

    # ---- Cost the FULL frame first, THEN filter ----
    # Marketing spend is allocated across each period's whole-store revenue, so
    # costing must see every line; filtering line items *before* costing would
    # dump a whole period's spend onto the slice (CM3 wrong). Every other cost is
    # per-line, so filtering the costed frame afterwards is exact.
    computed_all = margin.compute_costs(df_b, cfg, spend)
    computed_country = (computed_all if country == "All countries"
                        else computed_all[computed_all["country"] == country])
    computed = (computed_country[computed_country["bucket"].isin(sel_periods)]
                if sel_periods else computed_country)
    if computed.empty:
        st.warning("No orders match the current filters.")
        return

    agg = margin.aggregate(computed, cfg)
    agg = add_clusters(agg)
    # Δ CM3 vs the equivalent prior set of buckets (same country, immediately before)
    agg = _add_delta_cm3(agg, computed_country, sel_periods, cfg)

    # row-level filters
    if search.strip():
        s = search.strip().lower()
        agg = agg[agg["sku"].str.lower().str.contains(s, regex=False)
                  | agg["title"].astype(str).str.lower().str.contains(s, regex=False)]
    if min_sales > 0:
        agg = agg[agg["net_revenue"] >= min_sales]
    if top_only:
        agg = agg[agg["_v"] == 1]
    if agg.empty:
        st.warning("No SKUs match the filters.")
        return

    # ---- KPIs ----
    tgt3 = cfg["targets"]["cm3_pct"]
    total_sales = agg["net_revenue"].sum()
    pnl = agg["cm3"].sum()
    avg_cm3 = (pnl / total_sales * 100) if total_sales else float("nan")
    below = int((agg["cm3_pct"] < tgt3).sum())
    k = st.columns(5)
    k[0].metric("SKUs in view", f"{len(agg):,}")
    k[1].metric("Total sales (€)", f"{total_sales:,.0f}")
    k[2].metric("P&L Impact (€)", f"{pnl:,.0f}", help="Total CM3 € (absolute contribution after marketing).")
    k[3].metric("Avg CM3 %", f"{avg_cm3:.1f}" if pd.notna(avg_cm3) else "—")
    k[4].metric(f"SKUs below {tgt3:.0f}% CM3", f"{below:,}")

    if int(agg["cost_missing"].sum()):
        st.info(f"{int(agg['cost_missing'].sum())} SKU(s) missing a unit cost (COGS treated as €0). "
                "Add `Variant Cost` to the Matrixify Products export.")

    tab_ov, tab_tr, tab_sm = st.tabs(["Overview", "Margin Trend", "Slow movers"])

    with tab_ov:
        # Per-country breakdown
        if computed["country"].nunique() > 1 or (computed["country"].iloc[0] != "Unknown"):
            st.markdown("**Per-country breakdown** — Country CM3 % = total CM3 € / total Sales € for that country.")
            pc = (computed.groupby("country", as_index=False)
                  .agg(SKUs=("sku", "nunique"), Sales=("net_revenue", "sum"), Units=("net_qty", "sum"),
                       CM3=("cm3", "sum"), Ad=("marketing", "sum")))
            pc["Country CM3 %"] = pc["CM3"] / pc["Sales"].where(pc["Sales"] > 0) * 100
            pc = pc.sort_values("Sales", ascending=False).rename(
                columns={"Sales": "Sales (€)", "CM3": "P&L (€)", "Ad": "Ad spend (€)", "country": "Country"})
            st.dataframe(pc.style.format({"Sales (€)": "€{:,.0f}", "P&L (€)": "€{:,.0f}",
                                          "Ad spend (€)": "€{:,.0f}", "Units": "{:,.0f}",
                                          "Country CM3 %": "{:.1f}%"}),
                         use_container_width=True, hide_index=True)
        else:
            st.caption("ℹ️ Per-country breakdown unavailable — add a `Shipping: Country Code` "
                       "column to the Matrixify Orders export to enable it.")

        render_cluster_grid(agg)
        f = st.columns([3, 2])
        chosen = f[0].multiselect("Filter by cluster", sorted(agg["Cluster"].dropna().unique()))
        only_below = f[1].checkbox(f"Only SKUs with CM3 % below {tgt3:.0f}%", value=False)
        view = agg
        if chosen:
            view = view[view["Cluster"].isin(chosen)]
        if only_below:
            view = view[view["cm3_pct"] < tgt3]
        st.markdown(f"**{len(view):,} products**")
        render_table(view, cfg, key="overview")

    with tab_tr:
        render_trend(computed_country, tgt3)

    with tab_sm:
        st.markdown("**Slow movers** — lowest-selling SKUs in the current selection.")
        n = st.slider("Show N", 5, 50, 20)
        slow = agg.sort_values("net_revenue").head(n)
        render_table(slow, cfg, key="slow")

    with st.expander("📣 Marketing spend (Klar)"):
        if mkt_raw.empty:
            st.warning("No marketing file found.")
        else:
            cb = channel_breakdown(mkt_raw)
            st.dataframe(cb.style.format({"cost": "€{:,.0f}", "net_revenue": "€{:,.0f}"}),
                         use_container_width=True, hide_index=True)


def _add_delta_cm3(agg, costed_country, sel_periods, cfg):
    """Δ CM3 pp vs the equivalent prior set of buckets (same count, immediately
    before) — from the already-costed, same-country frame (no re-costing, so the
    baseline uses the same marketing denominator and the same country as `agg`)."""
    agg = agg.copy()
    agg["delta_cm3"] = np.nan
    if not sel_periods:
        return agg
    allb = ordered_buckets(costed_country)
    idx = [allb.index(b) for b in sel_periods if b in allb]
    if not idx:
        return agg
    k = len(sel_periods)
    prior = allb[max(0, min(idx) - k):min(idx)]
    if not prior:
        return agg
    pcomp = costed_country[costed_country["bucket"].isin(prior)]
    if pcomp.empty:
        return agg
    pagg = margin.aggregate(pcomp, cfg).set_index("sku")["cm3_pct"]
    agg["delta_cm3"] = agg["cm3_pct"] - agg["sku"].map(pagg)
    return agg


def render_trend(costed_country, tgt3):
    if costed_country is None or costed_country.empty:
        st.info("No data.")
        return
    comp = costed_country
    ts = (comp.groupby(["bucket", "bucket_start"], as_index=False)
          .agg(cm3=("cm3", "sum"), sales=("net_revenue", "sum")))
    ts["CM3 %"] = ts["cm3"] / ts["sales"].where(ts["sales"] > 0) * 100
    ts = ts.sort_values("bucket_start")
    fig = px.line(ts, x="bucket", y="CM3 %", markers=True, title="Portfolio CM3 % over time")
    fig.add_hline(y=tgt3, line_dash="dot", line_color="#d32f2f", annotation_text=f"Target {tgt3:.0f}%")
    fig.update_layout(height=380, xaxis_title="", yaxis_title="CM3 %")
    st.plotly_chart(fig, use_container_width=True)

    # per-SKU trend classification (linear fit of CM3% across buckets, sales-weighted)
    per = (comp.groupby(["sku", "bucket_start"], as_index=False)
           .agg(cm3=("cm3", "sum"), sales=("net_revenue", "sum")))
    per["cm3_pct"] = per["cm3"] / per["sales"].where(per["sales"] > 0) * 100
    rows = []
    for sku, g in per.dropna(subset=["cm3_pct"]).groupby("sku"):
        g = g.sort_values("bucket_start")
        if len(g) < 2:
            continue
        x = np.arange(len(g))
        slope = np.polyfit(x, g["cm3_pct"], 1, w=np.sqrt(g["sales"].clip(lower=0) + 1))[0]
        rows.append({"SKU": sku, "Start CM3 %": g["cm3_pct"].iloc[0], "End CM3 %": g["cm3_pct"].iloc[-1],
                     "Change pp": g["cm3_pct"].iloc[-1] - g["cm3_pct"].iloc[0], "Points": len(g), "_slope": slope})
    if not rows:
        st.caption("Need at least two periods of data per SKU for a trend.")
        return
    tr = pd.DataFrame(rows)
    thr = st.number_input("Trend threshold (pp)", value=2.0, step=0.5)
    up = int((tr["Change pp"] >= thr).sum()); down = int((tr["Change pp"] <= -thr).sum())
    flat = len(tr) - up - down
    c = st.columns(3)
    c[0].metric("📈 Improving", up); c[1].metric("➖ Stable", flat); c[2].metric("📉 Declining", down)
    st.dataframe(tr.drop(columns="_slope").sort_values("Change pp")
                 .style.format({"Start CM3 %": "{:.1f}%", "End CM3 %": "{:.1f}%", "Change pp": "{:+.1f}"}),
                 use_container_width=True, hide_index=True)


main()

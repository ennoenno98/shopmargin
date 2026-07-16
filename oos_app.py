"""Shopify OOS Impact Analytics — standalone dashboard.

A separate Streamlit app (deploy with main-file path = ``oos_app.py``), modelled
on the Amazon "OOS Impact Analytics" board: estimated lost revenue & lost
contribution margin (CM3) from being out of stock, over time and per SKU.

Stock is one global pool (no country split). It joins the committed daily stock
history (``data/stock_history.csv`` — see build_stock_history.py) with realised
sales/margin from the Matrixify Orders export (priced by margin.py). Sections:
the out-of-stock impact time chart + KPIs, then Most affected SKUs · Stock-out
calendar · Stock-out events. (No country view, no cooling-down / heating-up.)
"""
from __future__ import annotations

import os
from datetime import timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import data_source
import margin
import matrixify_client
import oos_impact
import stock_history
from config import load_config
from marketing import load_marketing, spend_table

st.set_page_config(page_title="Shopify OOS Impact Analytics", page_icon="📦", layout="wide")
CFG = load_config()
STOCK_HISTORY_FILE = "data/stock_history.csv"
ORANGE, RED, NAVY, GREY = "#F4A259", "#E76F51", "#1F3864", "#B7B7B7"


def _sig(cfg: dict) -> str:
    parts = []
    for p in ((cfg.get("source") or {}).get("orders_file", ""),
              (cfg.get("source") or {}).get("products_file", ""),
              cfg["marketing"]["spend_file"], STOCK_HISTORY_FILE):
        parts.append(f"{p}:{os.path.getmtime(p)}" if p and os.path.exists(p) else f"{p}:0")
    return "|".join(parts)


@st.cache_data(show_spinner="Loading & costing data…")
def load(orders_file: str, products_file: str, tz: str, spend_file: str, sig: str):
    cfg = {"source": {"orders_file": orders_file, "products_file": products_file}, "timezone": tz}
    df, mode = data_source.load_lineitems(cfg)
    inv = pd.DataFrame()
    if products_file and os.path.exists(products_file):
        try:
            inv = matrixify_client.load_inventory(products_file)
        except Exception:
            inv = pd.DataFrame()
    hist = stock_history.load_stock_history(STOCK_HISTORY_FILE)
    spend = spend_table(load_marketing(spend_file), CFG["marketing"].get("grain", "day"))
    costed = margin.compute_costs(df, CFG, spend) if not df.empty else df
    return mode, inv, hist, costed


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def impact_timechart(bb: pd.DataFrame):
    fig = go.Figure()
    fig.add_bar(x=bb["bucket"], y=bb["lost_revenue"], name="Lost revenue (€)", marker_color=ORANGE)
    fig.add_bar(x=bb["bucket"], y=bb["lost_cm3"], name="Lost CM3 (€)", marker_color=RED)
    fig.add_trace(go.Scatter(x=bb["bucket"], y=bb["oos_rate"], name="OOS rate (%)",
                             yaxis="y2", mode="lines+markers", line=dict(color=NAVY, width=2)))
    fig.update_layout(
        barmode="group", height=380, margin=dict(t=10, b=0, l=0, r=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        yaxis=dict(title="€ lost", rangemode="tozero"),
        yaxis2=dict(title="OOS rate (%)", overlaying="y", side="right", rangemode="tozero",
                    showgrid=False))
    return fig


def sku_timeline(hist_sku: pd.DataFrame, events_sku: pd.DataFrame, title: str):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hist_sku["date"], y=hist_sku["on_hand"], name="On-hand", mode="lines",
        line=dict(color=GREY, width=1.5), fill="tozeroy", fillcolor="rgba(183,183,183,0.25)"))
    # red bands over each out-of-stock run
    for _, e in events_sku.iterrows():
        fig.add_vrect(x0=pd.Timestamp(e["start"]), x1=pd.Timestamp(e["end"]) + pd.Timedelta(days=1),
                      fillcolor=RED, opacity=0.18, line_width=0)
    fig.add_hline(y=0, line_dash="dot", line_color=RED)
    fig.update_layout(height=300, margin=dict(t=30, b=0, l=0, r=0), title=title,
                      xaxis_title="", yaxis_title="On-hand units", showlegend=False)
    return fig


def oos_calendar(hist_f: pd.DataFrame, skus: list[str]):
    """Heatmap: rows = SKUs, cols = date, red where out of stock."""
    g = hist_f[hist_f["sku"].isin(skus)].copy()
    g["oos"] = (pd.to_numeric(g["on_hand"], errors="coerce") <= 0).astype(int)
    piv = (g.pivot_table(index="sku", columns="date", values="oos", aggfunc="max")
           .reindex(skus))
    if piv.empty:
        return None
    fig = go.Figure(go.Heatmap(
        z=piv.values, x=[pd.Timestamp(c).strftime("%Y-%m-%d") for c in piv.columns],
        y=list(piv.index), colorscale=[[0, "#E8F0E8"], [1, RED]], showscale=False,
        xgap=1, ygap=1))
    fig.update_layout(height=max(220, 26 * len(piv) + 80), margin=dict(t=10, b=0, l=0, r=0),
                      yaxis=dict(autorange="reversed"))
    return fig


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    src = CFG["source"]
    mode, inv, hist, costed = load(
        src["orders_file"], src["products_file"], CFG.get("timezone", "Europe/Berlin"),
        CFG["marketing"]["spend_file"], _sig(CFG))

    st.markdown("## 📦 OOS Impact Analytics")
    st.caption(
        "Estimated **lost revenue & contribution margin (CM3)** from being out of stock, "
        "over time and per SKU. Stock is one global pool (no country split). Built from the "
        "committed daily **stock history** + realised sales priced by the margin engine — "
        "the Shopify analogue of the Amazon OOS Impact board."
    )

    if hist is None or hist.empty:
        st.info(
            "No stock history yet. Run `python build_stock_history.py --backfill` to "
            "reconstruct it from the committed daily products snapshots (it then grows one "
            "day per refresh). For a deeper backfill, drop a Shopify Analytics `inventory` "
            "export into `data/stock_overview_seed.csv` and re-run with `--backfill`."
        )
        return
    if costed is None or costed.empty:
        st.info("No orders data available to price lost sales.")
        return

    titles = inv["title"].astype(str).to_dict() if inv is not None and not inv.empty else {}

    # source / freshness line
    span = f"{hist['date'].min():%Y-%m-%d} → {hist['date'].max():%Y-%m-%d}"
    srcs = ", ".join(sorted(hist["source"].dropna().unique())) or "—"
    try:
        refreshed = pd.Timestamp(os.path.getmtime(STOCK_HISTORY_FILE), unit="s").tz_localize("UTC")
        refresh_txt = f"{refreshed:%Y-%m-%d %H:%M UTC}"
    except OSError:
        refresh_txt = "—"
    st.caption(f"Stock history `{span}` · {hist['sku'].nunique():,} SKUs · source: {srcs} · "
               f"refreshed **{refresh_txt}**" + ("  ·  ⚠️ sample data" if mode == "sample" else ""))

    # ---- Filters ----
    with st.container(border=True):
        c = st.columns([1.5, 1.3, 1.6, 2])
        period = c[0].selectbox("Period", ["Full range", "Last 365 days", "Last 180 days",
                                           "Last 90 days", "Last 30 days"], index=0)
        gran = c[1].radio("Bucket", ["Month", "Quarter"], horizontal=True)
        min_demand = c[2].slider("Min demand (units/day)", 0.0, 10.0, 3.0, 0.5,
                                 help="Keep only SKUs whose all-history sales rate clears this floor.")
        search = c[3].text_input("SKU or Product contains", "")

    hist_f, costed_f = hist, costed
    if period != "Full range":
        days = {"Last 365 days": 365, "Last 180 days": 180, "Last 90 days": 90, "Last 30 days": 30}[period]
        cutoff = hist["date"].max() - pd.Timedelta(days=days)
        hist_f = hist[hist["date"] >= cutoff]
        cdate = pd.to_datetime(costed["order_date"], errors="coerce")
        costed_f = costed[cdate >= cutoff]

    imp = oos_impact.compute_impact(hist_f, costed_f, gran=gran, min_demand=min_demand)
    if search.strip():
        s = search.strip().lower()
        keep = {sku for sku in imp["sku"].unique()
                if s in sku.lower() or s in str(titles.get(sku, "")).lower()}
        imp = imp[imp["sku"].isin(keep)]

    # ---- KPIs ----
    t = oos_impact.totals(imp)
    k = st.columns(4)
    k[0].metric("SKUs affected", f"{t['skus_affected']:,}")
    k[1].metric("Lost revenue", f"€{t['lost_revenue']:,.0f}")
    k[2].metric("Lost CM3 (P&L impact)", f"€{t['lost_cm3']:,.0f}")
    k[3].metric("OOS rate", f"{t['oos_rate']:.1f} %",
                help="Share of demand lost to stock-outs = lost units / (sold + lost units).")

    if imp.empty:
        st.warning("Nothing to show for these filters — try lowering the min-demand floor or "
                   "widening the period. (Lost sales need overlap between stock history and orders.)")
        return

    # ---- Out-of-stock impact over time ----
    st.markdown("### 🔴 Out-of-stock impact over time")
    bb = oos_impact.by_bucket(imp)
    st.plotly_chart(impact_timechart(bb), use_container_width=True)

    sku_tot = oos_impact.by_sku(imp)
    events = oos_impact.stockout_events(hist_f, imp)

    tab_aff, tab_cal, tab_ev = st.tabs(["Most affected SKUs", "Stock-out calendar", "Stock-out events"])

    with tab_aff:
        n = st.slider("Show top N SKUs by lost CM3", 5, 50, min(15, max(5, len(sku_tot))))
        top = sku_tot.head(n).copy()
        top["label"] = top["sku"].map(lambda s: f"{s} · {str(titles.get(s, ''))[:32]}")
        bar = go.Figure(go.Bar(
            x=top["lost_cm3"], y=top["label"], orientation="h", marker_color=RED,
            hovertemplate="%{y}<br>Lost CM3 €%{x:,.0f}<extra></extra>"))
        bar.update_layout(height=max(260, 24 * len(top) + 80), margin=dict(t=10, b=0, l=0, r=0),
                          xaxis_title="Lost CM3 (€)", yaxis=dict(autorange="reversed"))
        st.plotly_chart(bar, use_container_width=True)

        st.markdown("**Timeline for a SKU** — grey = on-hand stock, red band = out of stock.")
        opts = top["sku"].tolist()
        if opts:
            sku = st.selectbox("SKU", opts, format_func=lambda s: f"{s} · {str(titles.get(s, ''))[:40]}")
            row = sku_tot[sku_tot["sku"] == sku].iloc[0]
            title = (f"{sku} · {str(titles.get(sku, ''))[:50]} — "
                     f"{int(row['oos_days'])} OOS days · lost €{row['lost_revenue']:,.0f} rev / "
                     f"€{row['lost_cm3']:,.0f} CM3")
            st.plotly_chart(
                sku_timeline(stock_history.series_for(hist_f, sku),
                             events[events["sku"] == sku], title),
                use_container_width=True)

    with tab_cal:
        st.markdown("**Stock-out calendar** — red = day out of stock, for the most-affected SKUs.")
        cal_skus = sku_tot.head(20)["sku"].tolist()
        fig = oos_calendar(hist_f, cal_skus)
        if fig is None:
            st.caption("No data for the calendar in this window.")
        else:
            st.plotly_chart(fig, use_container_width=True)

    with tab_ev:
        if events.empty:
            st.caption("No stock-out events in this window.")
        else:
            ev = events.copy()
            ev["Product"] = ev["sku"].map(lambda s: str(titles.get(s, "")))
            show = ev[["sku", "Product", "start", "end", "days", "lost_units",
                       "lost_revenue", "lost_cm3"]].rename(columns={
                "sku": "SKU", "start": "Out since", "end": "Until", "days": "Days OOS",
                "lost_units": "Lost units", "lost_revenue": "Lost rev (€)", "lost_cm3": "Lost CM3 (€)"})
            st.markdown(f"**{len(show):,} stock-out events** (newest first)")
            st.dataframe(
                show.style.format({"Out since": "{:%Y-%m-%d}", "Until": "{:%Y-%m-%d}",
                                   "Days OOS": "{:,.0f}", "Lost units": "{:,.0f}",
                                   "Lost rev (€)": "€{:,.0f}", "Lost CM3 (€)": "€{:,.0f}"}, na_rep="—"),
                use_container_width=True, hide_index=True, height=420)
            st.download_button("⬇️ Download events (CSV)", show.to_csv(index=False).encode("utf-8"),
                               "shopify_oos_events.csv", "text/csv")

    st.caption(
        "**Lost units** = the SKU's demand rate (units sold ÷ in-stock days) × days out of stock; "
        "**Lost revenue / CM3** value those at the SKU's average selling price and CM3 per unit. "
        "**OOS rate** = lost ÷ (sold + lost) units. Depth grows as the daily stock history "
        "accumulates (and with a deeper Shopify backfill seed)."
    )


main()

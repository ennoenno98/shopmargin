"""PDF report for the OOS Impact dashboard.

Pure functions (no Streamlit): :func:`build_report` takes the frames the app has
already computed for the current store / window / filters and returns the PDF as
bytes. Layout is fpdf2; charts are drawn with matplotlib and embedded as images.

Pages: summary (KPIs, lost € and OOS rate per bucket, notes) · most affected SKUs
· clusters (when a cluster mapping exists) · stock-out events.

Charts follow two rules that matter on paper: one y-axis per chart (lost € and
OOS rate are separate charts, not a dual axis), and a colour pair that stays
distinguishable for colour-blind readers and in greyscale-ish print.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from fpdf import FPDF  # noqa: E402
from fpdf.fonts import FontFace  # noqa: E402

# Ink, surfaces and series colours (validated pair: blue / orange).
INK, INK2, GRID, AXIS = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE = "#2a78d6", "#eb6834"
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"]   # one hue, light -> dark
TILE_BG, HEAD_BG = (244, 246, 249), (236, 239, 244)

_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
PAGE_W, MARGIN = 210, 14
CONTENT_W = PAGE_W - 2 * MARGIN


def eur(v) -> str:
    return "—" if v is None or pd.isna(v) else f"€{float(v):,.0f}"


def num(v, nd: int = 0) -> str:
    return "—" if v is None or pd.isna(v) else f"{float(v):,.{nd}f}"


def pct(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{float(v):.1f}%"


def _lc(label: str) -> str:
    """Lower-case only the first letter ("Lost CM3" -> "lost CM3")."""
    return label[:1].lower() + label[1:]


def _clip(text, n: int) -> str:
    text = "" if text is None or (isinstance(text, float) and pd.isna(text)) else str(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Charts (matplotlib -> PNG bytes)
# --------------------------------------------------------------------------- #
def _style(ax, *, grid_axis: str = "y"):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _png(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def chart_lost(bb: pd.DataFrame, margin_label: str) -> io.BytesIO:
    """Lost revenue and lost margin per bucket — grouped bars, one € axis."""
    fig, ax = plt.subplots(figsize=(7.4, 2.7))
    x = range(len(bb))
    w = 0.36
    b1 = ax.bar([i - w / 2 - 0.01 for i in x], bb["lost_revenue"], w, color=BLUE, label="Lost revenue")
    b2 = ax.bar([i + w / 2 + 0.01 for i in x], bb["lost_cm3"], w, color=ORANGE, label=margin_label)
    if len(bb) <= 8:                      # direct labels only while they stay readable
        for bars in (b1, b2):
            ax.bar_label(bars, labels=[eur(v) if v else "" for v in bars.datavalues],
                         padding=2, fontsize=7, color=INK2)
    ax.set_xticks(list(x), bb["bucket"].astype(str))
    ax.yaxis.set_major_formatter(lambda v, _: f"€{v:,.0f}")
    ax.set_ylim(0, max(float(bb["lost_revenue"].max()), float(bb["lost_cm3"].max()), 1) * 1.18)
    ax.legend(frameon=False, fontsize=8, loc="upper right", ncols=2, labelcolor=INK2)
    _style(ax)
    return _png(fig)


def chart_oos_rate(bb: pd.DataFrame) -> io.BytesIO:
    """OOS rate per bucket — its own chart and axis (never a second y-axis)."""
    fig, ax = plt.subplots(figsize=(7.4, 1.9))
    x = list(range(len(bb)))
    ax.plot(x, bb["oos_rate"], color=BLUE, linewidth=2, marker="o", markersize=5)
    if len(bb) <= 12:
        for i, v in zip(x, bb["oos_rate"]):
            ax.annotate(pct(v), (i, v), textcoords="offset points", xytext=(0, 7),
                        ha="center", fontsize=7, color=INK2)
    ax.set_xticks(x, bb["bucket"].astype(str))
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.set_ylim(0, max(float(bb["oos_rate"].max()), 1) * 1.35)
    ax.set_xlim(-0.5, max(len(bb) - 0.5, 0.5))
    _style(ax)
    return _png(fig)


def chart_top_skus(top: pd.DataFrame, margin_label: str) -> io.BytesIO:
    """Top SKUs by lost margin — horizontal bars, single series."""
    t = top.iloc[::-1]
    fig, ax = plt.subplots(figsize=(7.4, max(1.6, 0.27 * len(t) + 0.6)))
    bars = ax.barh(t["label"], t["lost_cm3"], height=0.62, color=BLUE)
    ax.bar_label(bars, labels=[eur(v) for v in t["lost_cm3"]], padding=3, fontsize=7, color=INK2)
    ax.xaxis.set_major_formatter(lambda v, _: f"€{v:,.0f}")
    ax.set_xlim(0, max(float(t["lost_cm3"].max()), 1) * 1.15)
    ax.set_xlabel(margin_label, fontsize=8, color=INK2)
    _style(ax, grid_axis="x")
    return _png(fig)


def chart_matrix(mx: pd.DataFrame, rows: list[str], cols: list[str], margin_label: str) -> io.BytesIO:
    """Cluster × OOS-severity matrix — one-hue sequential fill by lost margin."""
    val = mx.pivot(index="cluster", columns="tier", values="lost_cm3").reindex(index=rows, columns=cols).fillna(0.0)
    cnt = mx.pivot(index="cluster", columns="tier", values="skus").reindex(index=rows, columns=cols).fillna(0)
    top = float(val.values.max()) or 1.0
    fig, ax = plt.subplots(figsize=(7.4, 0.62 * len(rows) + 0.7))
    for r, row in enumerate(rows):
        for c, col in enumerate(cols):
            n, v = int(cnt.loc[row, col]), float(val.loc[row, col])
            if n == 0:
                fill, ink = "#f7f7f5", INK2
            else:
                step = min(int(v / top * (len(SEQ) - 1) + 0.5), len(SEQ) - 1) if v > 0 else 0
                fill, ink = SEQ[step], ("white" if step >= 3 else INK)
            ax.add_patch(plt.Rectangle((c + 0.03, r + 0.06), 0.94, 0.88, color=fill, linewidth=0))
            if n:
                ax.text(c + 0.5, r + 0.40, eur(v), ha="center", va="center", fontsize=9,
                        color=ink, fontweight="bold")
                ax.text(c + 0.5, r + 0.70, f"{n} SKU" + ("" if n == 1 else "s"), ha="center",
                        va="center", fontsize=7.5, color=ink)
    ax.set_xlim(0, len(cols)); ax.set_ylim(len(rows), 0)
    ax.set_xticks([c + 0.5 for c in range(len(cols))], cols)
    ax.set_yticks([r + 0.5 for r in range(len(rows))], [f"Cluster {r}" for r in rows])
    ax.xaxis.tick_top()
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    return _png(fig)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
class _Report(FPDF):
    def __init__(self, footer_text: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self._footer_text = footer_text
        self.add_font("DejaVu", "", os.path.join(_FONT_DIR, "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf"))
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(True, margin=16)

    def footer(self):
        self.set_y(-11)
        self.set_font("DejaVu", size=7)
        self.set_text_color(110, 110, 105)
        self.cell(CONTENT_W / 2, 4, self._footer_text)
        self.cell(CONTENT_W / 2, 4, f"Page {self.page_no()}", align="R")

    # -- small building blocks -------------------------------------------------
    def h1(self, text: str):
        self.set_font("DejaVu", "B", 17); self.set_text_color(11, 11, 11)
        self.cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")

    def h2(self, text: str):
        self.ln(2)
        self.set_font("DejaVu", "B", 11.5); self.set_text_color(11, 11, 11)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")

    def body(self, text: str, size: float = 8.5, color=(82, 81, 78)):
        self.set_font("DejaVu", size=size); self.set_text_color(*color)
        self.multi_cell(0, size * 0.48, text, new_x="LMARGIN", new_y="NEXT")

    def kpi_row(self, tiles: list[tuple[str, str]]):
        gap, h = 3.0, 19.0
        w = (CONTENT_W - gap * (len(tiles) - 1)) / len(tiles)
        y = self.get_y() + 1
        for i, (label, value) in enumerate(tiles):
            x = MARGIN + i * (w + gap)
            self.set_fill_color(*TILE_BG)
            self.rect(x, y, w, h, style="F", round_corners=True, corner_radius=1.5)
            self.set_xy(x + 3, y + 3)
            self.set_font("DejaVu", size=7.5); self.set_text_color(82, 81, 78)
            self.cell(w - 6, 4, label)
            self.set_xy(x + 3, y + 8.5)
            self.set_font("DejaVu", "B", 14); self.set_text_color(11, 11, 11)
            self.cell(w - 6, 7, value)
        self.set_y(y + h + 3)

    def chart(self, png: io.BytesIO, w: float = CONTENT_W):
        self.image(png, x=MARGIN, w=w)
        self.ln(1.5)

    def table(self, header: list[str], rows: list[list[str]], widths: list[float], aligns: list[str]):
        self.set_font("DejaVu", size=7.5); self.set_text_color(11, 11, 11)
        self.set_draw_color(225, 224, 217)
        head = FontFace(emphasis="BOLD", fill_color=HEAD_BG, color=(11, 11, 11))
        with super().table(col_widths=tuple(widths), text_align=tuple(aligns), line_height=4.6,
                           borders_layout="HORIZONTAL_LINES", headings_style=head,
                           width=CONTENT_W, align="LEFT", padding=(0.6, 1.2)) as t:
            t.row(header)
            for r in rows:
                t.row(r)
        self.ln(1)


def build_report(*, store_label: str, window: tuple[str, str], filters: dict[str, str],
                 totals: dict, by_bucket: pd.DataFrame, sku_view: pd.DataFrame, titles: dict,
                 events: pd.DataFrame, notes: list[str] | None = None,
                 margin_label: str = "Lost CM3", cluster_matrix: pd.DataFrame | None = None,
                 cluster_summary: pd.DataFrame | None = None, cluster_rows: list[str] | None = None,
                 tier_cols: list[str] | None = None, top_n: int = 20, max_events: int = 30,
                 generated: datetime | None = None) -> bytes:
    """Render the current OOS view as a PDF. ``sku_view`` is ``oos_impact.by_sku``
    output (sorted by lost margin, optionally with ``cluster`` / ``oos_rate``)."""
    generated = generated or datetime.now(timezone.utc)
    stamp = f"{generated:%Y-%m-%d %H:%M} UTC"
    pdf = _Report(f"OOS Impact · {store_label} · {window[0]} → {window[1]} · generated {stamp}")
    has_cluster = "cluster" in sku_view.columns

    # ---- Page 1: summary ----
    pdf.add_page()
    pdf.h1(f"OOS Impact Report — {store_label}")
    pdf.body(f"Window {window[0]} → {window[1]}   ·   generated {stamp}", size=9)
    pdf.body("   ·   ".join(f"{k}: {v}" for k, v in filters.items()), size=8)
    pdf.ln(1)
    pdf.kpi_row([("SKUs affected", num(totals.get("skus_affected"))),
                 ("Lost revenue", eur(totals.get("lost_revenue"))),
                 (margin_label, eur(totals.get("lost_cm3"))),
                 ("OOS rate", pct(totals.get("oos_rate")))])
    if by_bucket is not None and not by_bucket.empty:
        pdf.h2(f"Lost revenue and {_lc(margin_label)} over time")
        pdf.chart(chart_lost(by_bucket, margin_label))
        pdf.h2("OOS rate over time")
        pdf.body("Share of demand lost to stock-outs = lost units ÷ (sold + lost units).", size=7.5)
        pdf.chart(chart_oos_rate(by_bucket))
        pdf.table(["Period", "Units sold", "Lost units", "Lost revenue", margin_label, "OOS rate"],
                  [[str(r.bucket), num(r.units), num(r.lost_units), eur(r.lost_revenue),
                    eur(r.lost_cm3), pct(r.oos_rate)] for r in by_bucket.itertuples()],
                  [30, 26, 26, 34, 36, 30], ["LEFT"] + ["RIGHT"] * 5)
    if notes:
        pdf.h2("Read this with")
        for n in notes:
            pdf.body("•  " + n, size=8)

    # ---- Page 2: most affected SKUs ----
    pdf.add_page()
    pdf.h2(f"Most affected SKUs — by {_lc(margin_label)}")
    hit = sku_view[sku_view["lost_units"] > 0].head(top_n).copy()
    if hit.empty:
        pdf.body("No SKU lost sales to a stock-out in this view.")
    else:
        chart_rows = hit.head(12).copy()
        chart_rows["label"] = [_clip(f"{s} · {titles.get(s, '')}".rstrip(" ·"), 44) for s in chart_rows["sku"]]
        pdf.chart(chart_top_skus(chart_rows, margin_label))
        header = ["SKU", "Product"] + (["Cl."] if has_cluster else []) + \
                 ["OOS days", "Lost units", "Lost revenue", margin_label, "OOS rate"]
        widths = [34, 50 if has_cluster else 60] + ([10] if has_cluster else []) + [17, 19, 24, 24, 14]
        aligns = ["LEFT", "LEFT"] + (["LEFT"] if has_cluster else []) + ["RIGHT"] * 5
        rows = []
        for r in hit.itertuples():
            demand = r.units + r.lost_units
            rows.append([_clip(r.sku, 24), _clip(titles.get(r.sku, ""), 36)]
                        + ([_clip(r.cluster, 5)] if has_cluster else [])
                        + [num(r.oos_days), num(r.lost_units), eur(r.lost_revenue), eur(r.lost_cm3),
                           pct(r.lost_units / demand * 100 if demand > 0 else 0)])
        pdf.table(header, rows, widths, aligns)
        left = int((sku_view["lost_units"] > 0).sum()) - len(hit)
        if left > 0:
            pdf.body(f"+ {left} more affected SKU(s) not listed.", size=7.5)

    # ---- Page 3: clusters ----
    if cluster_matrix is not None and cluster_summary is not None and not cluster_summary.empty:
        pdf.add_page()
        pdf.h2("Clusters — ABCD × OOS severity")
        pdf.body("Each cell holds the SKUs of that cluster whose share of demand lost to "
                 f"stock-outs falls in that band. Cell value = {_lc(margin_label)}; darker = more lost.",
                 size=8)
        pdf.chart(chart_matrix(cluster_matrix, cluster_rows or [], tier_cols or [], margin_label))
        pdf.h2("Cluster summary")
        pdf.table(["Cluster", "SKUs", "Went OOS", "OOS rate", "Lost revenue", margin_label, "Share"],
                  [[str(r.cluster), num(r.skus), num(r.skus_oos), pct(r.oos_rate), eur(r.lost_revenue),
                    eur(r.lost_cm3), pct(r.share_lost_cm3)] for r in cluster_summary.itertuples()],
                  [34, 18, 22, 24, 30, 30, 24], ["LEFT"] + ["RIGHT"] * 6)

    # ---- Page 4: stock-out events ----
    pdf.add_page()
    pdf.h2("Stock-out events — largest by lost revenue")
    if events is None or events.empty:
        pdf.body("No stock-out events in this window.")
    else:
        ev = events.sort_values("lost_revenue", ascending=False).head(max_events)
        pdf.table(["SKU", "Product", "Out since", "Until", "Days", "Lost units", "Lost revenue", margin_label],
                  [[_clip(r.sku, 24), _clip(titles.get(r.sku, ""), 30), f"{pd.Timestamp(r.start):%Y-%m-%d}",
                    f"{pd.Timestamp(r.end):%Y-%m-%d}", num(r.days), num(r.lost_units),
                    eur(r.lost_revenue), eur(r.lost_cm3)] for r in ev.itertuples()],
                  [34, 42, 21, 21, 11, 18, 23, 22], ["LEFT", "LEFT", "LEFT", "LEFT"] + ["RIGHT"] * 4)
        if len(events) > len(ev):
            pdf.body(f"Showing the {len(ev)} largest of {len(events):,} events.", size=7.5)
    pdf.ln(2)
    pdf.body("Method: lost units = the SKU's demand rate (units sold ÷ in-stock days) × days out of "
             "stock; lost revenue and margin value those at the SKU's average selling price and "
             "margin per unit. Days without a stock reading are left out.", size=7.5)
    return bytes(pdf.output())


if __name__ == "__main__":
    bb = pd.DataFrame({"bucket": ["Aug 2026", "Sep 2026", "Oct 2026"], "units": [6217, 11901, 10008],
                       "lost_units": [2145.1, 1793.3, 0.0], "lost_revenue": [43837.4, 35776.8, 0.0],
                       "lost_cm3": [36835.7, 31348.7, 0.0], "oos_rate": [25.7, 13.1, 0.0]})
    sv = pd.DataFrame({"sku": ["A-1", "B-2", "C-3"], "units": [1614, 401, 900], "oos_days": [61, 31, 0],
                       "lost_units": [3646.4, 200.5, 0.0], "lost_revenue": [72746.1, 5802.7, 0.0],
                       "lost_cm3": [63742.3, 3851.2, 0.0], "cluster": ["A", "C", "B"]})
    ev = pd.DataFrame({"sku": ["A-1", "B-2"], "start": pd.to_datetime(["2026-08-01", "2026-08-01"]),
                       "end": pd.to_datetime(["2026-09-30", "2026-08-31"]), "days": [61, 31],
                       "lost_units": [3646.4, 200.5], "lost_revenue": [72746.1, 5802.7],
                       "lost_cm3": [63742.3, 3851.2]})
    tiers = ["In stock", "Low (<10%)", "Medium (10–30%)", "High (≥30%)"]
    mx = pd.DataFrame([(c, t, 0, 0.0, 0.0) for c in "ABCD" for t in tiers],
                      columns=["cluster", "tier", "skus", "lost_revenue", "lost_cm3"])
    mx.loc[(mx.cluster == "A") & (mx.tier == tiers[3]), ["skus", "lost_cm3"]] = [1, 63742.3]
    mx.loc[(mx.cluster == "C") & (mx.tier == tiers[3]), ["skus", "lost_cm3"]] = [1, 3851.2]
    mx.loc[(mx.cluster == "B") & (mx.tier == tiers[0]), ["skus"]] = [1]
    sm = pd.DataFrame({"cluster": ["A", "B", "C"], "skus": [1, 1, 1], "skus_oos": [1, 0, 1],
                       "oos_rate": [69.3, 0.0, 33.3], "lost_revenue": [72746.1, 0.0, 5802.7],
                       "lost_cm3": [63742.3, 0.0, 3851.2], "share_lost_cm3": [94.3, 0.0, 5.7]})
    out = build_report(
        store_label="Test store", window=("2026-08-01", "2026-11-11"),
        filters={"Bucket": "Month", "Min demand": "3.0 units/day", "Clusters": "all"},
        totals={"skus_affected": 2, "lost_revenue": 78548.8, "lost_cm3": 67593.5, "oos_rate": 10.9},
        by_bucket=bb, sku_view=sv, titles={"A-1": "Omega-3 Gummies – sugar-free (€)", "B-2": "Probio Sticks"},
        events=ev, notes=["Margin is an estimate."], cluster_matrix=mx, cluster_summary=sm,
        cluster_rows=list("ABCD"), tier_cols=tiers)
    assert out[:5] == b"%PDF-" and len(out) > 20_000
    assert out.count(b"/Type /Page\n") + out.count(b"/Type /Page ") + out.count(b"/Type /Page>") >= 1
    # no clusters, no events, single bucket, empty SKU list -> still renders
    out2 = build_report(store_label="Empty", window=("2026-01-01", "2026-01-31"), filters={},
                        totals={"skus_affected": 0, "lost_revenue": 0, "lost_cm3": 0, "oos_rate": 0},
                        by_bucket=bb.head(1), sku_view=sv.drop(columns="cluster").assign(lost_units=0.0),
                        titles={}, events=ev.head(0))
    assert out2[:5] == b"%PDF-"
    if len(__import__("sys").argv) > 1:
        open(__import__("sys").argv[1], "wb").write(out)
    print(f"oos_report self-test OK ({len(out):,} bytes)")

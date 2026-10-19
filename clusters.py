"""SKU clusters (JTL "ABCD-Cluster") for the OOS dashboard.

Pure functions (no Streamlit). The cluster assignment comes from a JTL-Wawi
export of custom article fields ("Eigene Felder Artikel"): semicolon-separated,
Windows-1252, long format — one row per (Artikelnummer, field, value). Only the
``ABCD-Cluster`` field is used. ``python clusters.py --import <jtl.csv>``
normalises such an export into ``data/sku_clusters.csv`` (sku, name, cluster),
which the dashboard reads.

Clusters: A/B/C/D (importance tiers) plus the lifecycle states NPL (new product
launch) and EOL (end of life). SKUs without a cluster (e.g. bundle sets) are
"Unclustered".

The OOS matrix crosses the four ABCD tiers with four OOS-severity tiers, where
severity is the SKU's OOS rate = lost units / (sold + lost units).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

CLUSTER_FILE = "data/sku_clusters.csv"
ABCD = ["A", "B", "C", "D"]
LIFECYCLE = ["NPL", "EOL"]
UNCLUSTERED = "Unclustered"
CLUSTER_ORDER = ABCD + LIFECYCLE + [UNCLUSTERED]

# OOS-severity tiers on the SKU's OOS rate (% of demand lost).
TIERS = ["In stock", "Low (<10%)", "Medium (10–30%)", "High (≥30%)"]
TIER_EDGES = (10.0, 30.0)


def parse_jtl(path) -> pd.DataFrame:
    """JTL custom-fields export -> (sku, name, cluster), one row per SKU."""
    try:
        raw = pd.read_csv(path, sep=";", encoding="cp1252", dtype=str, header=None)
    except UnicodeDecodeError:
        raw = pd.read_csv(path, sep=";", encoding="utf-8", dtype=str, header=None)
    raw = raw.iloc[1:, :4]          # drop header row and the trailing empty column
    raw.columns = ["sku", "name", "field", "value"]
    c = raw[raw["field"].str.strip().eq("ABCD-Cluster")].copy()
    c["sku"] = c["sku"].str.strip()
    c["cluster"] = c["value"].str.strip().str.upper()
    c = c[c["sku"].ne("") & c["cluster"].isin(ABCD + LIFECYCLE)]
    return (c[["sku", "name", "cluster"]].drop_duplicates("sku", keep="last")
            .sort_values("sku").reset_index(drop=True))


def load_clusters(path=CLUSTER_FILE) -> dict[str, str]:
    """sku -> cluster from the normalised CSV ({} if absent)."""
    try:
        df = pd.read_csv(path, dtype=str)
    except (FileNotFoundError, OSError, pd.errors.EmptyDataError):
        return {}
    return dict(zip(df["sku"].str.strip(), df["cluster"].str.strip()))


def assign(skus: pd.Series, mapping: dict[str, str]) -> pd.Series:
    return skus.astype(str).map(mapping).fillna(UNCLUSTERED)


def tier(oos_rate: pd.Series) -> pd.Series:
    """OOS rate (%) -> severity tier; no lost demand = "In stock"."""
    lo, hi = TIER_EDGES
    r = pd.to_numeric(oos_rate, errors="coerce").fillna(0.0)
    out = pd.Series(TIERS[3], index=r.index)
    out[r < hi] = TIERS[2]
    out[r < lo] = TIERS[1]
    out[r <= 0] = TIERS[0]
    return out


def sku_view(sku_tot: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """oos_impact.by_sku output + cluster, OOS rate (%) and severity tier."""
    v = sku_tot.copy()
    v["cluster"] = assign(v["sku"], mapping)
    demand = v["units"] + v["lost_units"]
    v["oos_rate"] = (v["lost_units"] / demand.where(demand > 0) * 100).fillna(0.0)
    v["tier"] = tier(v["oos_rate"])
    return v


def matrix(view: pd.DataFrame, rows=ABCD) -> pd.DataFrame:
    """Long cluster × tier grid (every cell present) with SKU counts and € lost."""
    g = (view[view["cluster"].isin(rows)]
         .groupby(["cluster", "tier"], as_index=False)
         .agg(skus=("sku", "nunique"), lost_revenue=("lost_revenue", "sum"),
              lost_cm3=("lost_cm3", "sum")))
    grid = pd.MultiIndex.from_product([rows, TIERS], names=["cluster", "tier"]).to_frame(index=False)
    return grid.merge(g, on=["cluster", "tier"], how="left").fillna(
        {"skus": 0, "lost_revenue": 0.0, "lost_cm3": 0.0})


def summary(view: pd.DataFrame) -> pd.DataFrame:
    """Per-cluster totals, in CLUSTER_ORDER."""
    g = view.groupby("cluster").agg(
        skus=("sku", "nunique"), skus_oos=("lost_units", lambda s: int((s > 0).sum())),
        units=("units", "sum"), lost_units=("lost_units", "sum"),
        lost_revenue=("lost_revenue", "sum"), lost_cm3=("lost_cm3", "sum"))
    demand = g["units"] + g["lost_units"]
    g["oos_rate"] = (g["lost_units"] / demand.where(demand > 0) * 100).fillna(0.0)
    tot = g["lost_cm3"].sum()
    g["share_lost_cm3"] = g["lost_cm3"] / tot * 100 if tot else 0.0
    return g.reindex([c for c in CLUSTER_ORDER if c in g.index]).reset_index()


# --------------------------------------------------------------------------- #
# CLI:  python clusters.py --import <jtl.csv>   |   python clusters.py  (self-test)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--import":
        out = parse_jtl(sys.argv[2])
        Path(CLUSTER_FILE).parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(CLUSTER_FILE, index=False)
        print(f"wrote {CLUSTER_FILE}: {len(out)} SKUs · "
              + " · ".join(f"{k} {v}" for k, v in out["cluster"].value_counts().items()))
        raise SystemExit(0)

    st = pd.DataFrame({
        "sku": ["a1", "a2", "b1", "c1", "n1", "x1"],
        "units": [100, 50, 80, 10, 5, 20], "lost_units": [0, 5, 40, 10, 0, 1],
        "lost_revenue": [0, 50, 400, 100, 0, 10], "lost_cm3": [0, 20, 160, 40, 0, 4],
        "oos_days": [0, 2, 9, 12, 0, 1]})
    m = {"a1": "A", "a2": "A", "b1": "B", "c1": "C", "n1": "NPL"}
    v = sku_view(st, m)
    assert v.set_index("sku")["cluster"]["x1"] == UNCLUSTERED
    assert list(v.set_index("sku").loc[["a1", "a2", "b1", "c1"], "tier"]) == \
        [TIERS[0], TIERS[1], TIERS[3], TIERS[3]]          # 0% · 9.1% · 33% · 50%
    mx = matrix(v)
    assert len(mx) == 16 and mx["skus"].sum() == 4
    assert mx.set_index(["cluster", "tier"]).loc[("B", TIERS[3]), "lost_cm3"] == 160
    sm = summary(v).set_index("cluster")
    assert list(sm.index) == ["A", "B", "C", "NPL", UNCLUSTERED]
    assert sm.loc["A", "skus_oos"] == 1 and abs(sm["share_lost_cm3"].sum() - 100) < 1e-9
    print("clusters self-test OK")

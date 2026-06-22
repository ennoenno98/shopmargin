"""Build / refresh ``data/stock_history.csv`` — the dashboard's stock time series.

The Matrixify Products export is overwritten daily, so it only holds the current
on-hand. This script assembles a real per-SKU daily series from three sources
(merged, newest-wins per date+SKU):

  1. **git backfill** (``--backfill``) — every committed daily products snapshot is
     a dated stock reading. Walks the git history of the products CSV. Reliable,
     all SKUs, no credentials; depth = however many daily commits exist.
  2. **daily append** (default) — appends *today's* snapshot from the current
     products file. This is what the GitHub Action runs after each refresh, so the
     series grows by one reading/SKU per day. Works with a shallow CI clone.
  3. **seed** (``--seed FILE``) — a deeper one-off backfill exported from Shopify
     Analytics' ``inventory`` dataset. Long format: a date column, a SKU column
     and an ``ending_inventory_units`` (or qty) column — exactly what this
     ShopifyQL query returns (export it to CSV and drop it in):

         FROM inventory SHOW ending_inventory_units
         GROUP BY product_variant_sku TIMESERIES day SINCE -300d UNTIL today

     Column names are matched tolerantly; seed has lowest priority so live
     git/append readings win on overlapping dates.

Usage
-----
    python build_stock_history.py --backfill            # rebuild from git (+ seed)
    python build_stock_history.py                       # append today's snapshot
    python build_stock_history.py --seed data/stock_overview_seed.csv --backfill
    python build_stock_history.py --self-test           # offline checks
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

import matrixify_client
import stock_history

DEFAULT_PRODUCTS = "data/matrixify_products.csv"
DEFAULT_OUT = "data/stock_history.csv"
DEFAULT_SEED = "data/stock_overview_seed.csv"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _long_from_inventory(inv: pd.DataFrame, when, source: str) -> pd.DataFrame:
    """inv = matrixify_client.inventory_from_frame output (indexed by sku)."""
    if inv is None or inv.empty:
        return pd.DataFrame(columns=stock_history.HISTORY_COLS)
    return pd.DataFrame({
        "date": pd.Timestamp(when).normalize(),
        "sku": inv.index.astype(str),
        "on_hand": pd.to_numeric(inv["on_hand"], errors="coerce").values,
        "source": source,
    }).dropna(subset=["on_hand"])


# --------------------------------------------------------------------------- #
# Source 1: git history of the products CSV
# --------------------------------------------------------------------------- #
def _git(args: list[str], repo: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout


def iter_git_snapshots(repo: str, relpath: str):
    """Yield (date, csv_text) for the newest commit of ``relpath`` on each date,
    across all refs, oldest date first."""
    out = _git(["log", "--all", "--follow", "--date=short",
                "--format=%H\t%ad", "--", relpath], repo)
    seen: dict[str, str] = {}
    # git log is newest-first; keep the first (newest) commit we see per date
    for line in out.splitlines():
        if "\t" not in line:
            continue
        h, d = line.split("\t", 1)
        seen.setdefault(d.strip(), h.strip())
    for d in sorted(seen):
        try:
            text = _git(["show", f"{seen[d]}:{relpath}"], repo)
        except subprocess.CalledProcessError:
            continue
        yield d, text


def from_git(repo: str, relpath: str) -> pd.DataFrame:
    frames = []
    for d, text in iter_git_snapshots(repo, relpath):
        try:
            inv = matrixify_client.inventory_from_frame(pd.read_csv(io.StringIO(text), low_memory=False))
        except Exception as exc:  # malformed historical revision — skip, keep going
            print(f"  · skip {d}: {exc}", file=sys.stderr)
            continue
        frames.append(_long_from_inventory(inv, d, "git"))
    built = stock_history.assemble(frames)
    print(f"git backfill: {built['date'].nunique() if not built.empty else 0} dates, "
          f"{len(built):,} rows")
    return built


# --------------------------------------------------------------------------- #
# Source 2: today's snapshot from the current products file
# --------------------------------------------------------------------------- #
def from_products_file(path: str, when=None) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=stock_history.HISTORY_COLS)
    if when is None:
        try:
            when = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).date()
        except OSError:
            when = datetime.now(timezone.utc).date()
    return _long_from_inventory(matrixify_client.load_inventory(p), when, "matrixify")


# --------------------------------------------------------------------------- #
# Source 3: optional deep seed (Shopify Analytics `inventory` export)
# --------------------------------------------------------------------------- #
def from_seed(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=stock_history.HISTORY_COLS)
    df = pd.read_csv(p)
    norm = {matrixify_client._norm(c): c for c in df.columns}

    def pick(*cands):
        for c in cands:
            hit = norm.get(matrixify_client._norm(c))
            if hit:
                return hit
        return None

    c_date = pick("day", "date", "month")
    c_sku = pick("product_variant_sku", "variant sku", "sku")
    c_qty = pick("ending_inventory_units", "ending inventory units", "on_hand",
                 "variant inventory qty", "inventory", "qty", "quantity", "units")
    if not (c_date and c_sku and c_qty):
        raise ValueError(
            f"Seed {path} needs date + SKU + qty columns; got {list(df.columns)}")
    out = pd.DataFrame({
        "date": pd.to_datetime(df[c_date], errors="coerce"),
        "sku": df[c_sku].astype(str).str.strip(),
        "on_hand": pd.to_numeric(df[c_qty], errors="coerce"),
        "source": "shopifyql",
    })
    out = stock_history.assemble([out])
    print(f"seed: {len(out):,} rows from {path} ({c_date}/{c_sku}/{c_qty})")
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build(repo=".", products=DEFAULT_PRODUCTS, out=DEFAULT_OUT, seed=DEFAULT_SEED,
          backfill=False, when=None) -> pd.DataFrame:
    # Lowest priority first so newer/live readings win on a (date, SKU) collision:
    #   seed  <  git backfill  <  existing committed history  <  today's snapshot
    frames = [from_seed(seed)]
    if backfill:
        frames.append(from_git(repo, products))
    frames.append(stock_history.load_stock_history(out))   # keep what's already committed
    frames.append(from_products_file(products, when))      # today
    built = stock_history.assemble(frames)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    # Write tidy integer on-hand (no "0.0"); normalized dates serialise as YYYY-MM-DD.
    out_df = built.assign(on_hand=built["on_hand"].round().astype("Int64")) if not built.empty else built
    out_df.to_csv(out, index=False)
    return built


def _self_test():
    # seed parser tolerates ShopifyQL column names; assemble priority holds.
    seed = pd.DataFrame({"day": ["2026-06-01", "2026-06-02"],
                         "product_variant_sku": ["A", "A"],
                         "ending_inventory_units": [5, 0]})
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "seed.csv"
        seed.to_csv(sp, index=False)
        s = from_seed(str(sp))
        assert list(s.columns) == stock_history.HISTORY_COLS
        assert s["on_hand"].tolist() == [5.0, 0.0], s["on_hand"].tolist()
        assert (s["source"] == "shopifyql").all()
    # newer source overrides seed on overlap
    git_like = pd.DataFrame({"date": ["2026-06-02"], "sku": ["A"], "on_hand": [99],
                             "source": ["git"]})
    merged = stock_history.assemble([s, git_like])
    got = merged[(merged["sku"] == "A") & (merged["date"] == pd.Timestamp("2026-06-02"))]
    assert got["on_hand"].iloc[0] == 99, got
    print("build_stock_history self-test OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--products", default=DEFAULT_PRODUCTS)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--seed", default=DEFAULT_SEED)
    ap.add_argument("--backfill", action="store_true",
                    help="rebuild the full series from git history (+ seed)")
    ap.add_argument("--date", default=None, help="override the snapshot date (YYYY-MM-DD)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        _self_test()
    else:
        when = date.fromisoformat(a.date) if a.date else None
        built = build(a.repo, a.products, a.out, a.seed, a.backfill, when)
        nd = built["date"].nunique() if not built.empty else 0
        rng = (f"{built['date'].min():%Y-%m-%d}..{built['date'].max():%Y-%m-%d}"
               if not built.empty else "—")
        print(f"wrote {a.out}: {len(built):,} rows · {built['sku'].nunique() if not built.empty else 0} "
              f"SKUs · {nd} dates · {rng}")

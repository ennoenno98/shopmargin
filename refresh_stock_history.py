"""Deep-backfill the stock history straight from the Shopify Admin API.

Pulls per-SKU daily ``ending_inventory_units`` from Shopify Analytics' ShopifyQL
``inventory`` dataset and writes a seed CSV that ``build_stock_history.py``
ingests. This is the reliable way to land the deep history (~10 months): the
pull happens server-side in one run — no thousands of rows relayed by hand.

Run it once (locally or via the backfill_stock_history.yml workflow):

    export SHOPIFY_SHOP=vegavero.myshopify.com
    export SHOPIFY_ADMIN_TOKEN=shpat_...        # Admin API access token, read_reports scope
    python refresh_stock_history.py --since 2025-09-01
    python build_stock_history.py --backfill    # merge the seed into data/stock_history.csv

The ShopifyQL it runs (validated against the live store):

    FROM inventory SHOW ending_inventory_units
    GROUP BY product_variant_sku TIMESERIES day SINCE <d0> UNTIL <d1>

queried in short date windows so each response stays under the row cap, then
stitched. Offline checks: ``python refresh_stock_history.py --self-test``.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

API_VERSION = "2024-10"
ROW_CAP = 1000  # ShopifyQL tabular cap; we window dates to stay under it
DEFAULT_OUT = "data/stock_overview_seed.csv"
QUERY = ("FROM inventory SHOW ending_inventory_units "
         "GROUP BY product_variant_sku TIMESERIES day SINCE {since} UNTIL {until}")
_GQL = "query($q:String!){shopifyqlQuery(query:$q){parseErrors tableData{columns{name} rowData}}}"


def _endpoint(shop: str) -> str:
    shop = shop.strip()
    if not shop.endswith(".myshopify.com"):
        shop = f"{shop}.myshopify.com"
    return f"https://{shop}/admin/api/{API_VERSION}/graphql.json"


def run_shopifyql(shopql: str, shop: str, token: str, session: requests.Session) -> pd.DataFrame:
    """Execute one ShopifyQL query, return a tidy (day, sku, on_hand) frame."""
    r = session.post(_endpoint(shop), headers={"X-Shopify-Access-Token": token,
                     "Content-Type": "application/json"},
                     json={"query": _GQL, "variables": {"q": shopql}}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    res = (body.get("data") or {}).get("shopifyqlQuery") or {}
    if res.get("parseErrors"):
        raise RuntimeError(f"ShopifyQL parse errors: {res['parseErrors']}")
    return _parse_table(res.get("tableData") or {})


def _parse_table(table: dict) -> pd.DataFrame:
    cols = [c["name"] for c in (table.get("columns") or [])]
    rows = table.get("rowData") or []
    if not cols or not rows:
        return pd.DataFrame(columns=["day", "sku", "on_hand"])
    df = pd.DataFrame(rows, columns=cols)
    day = next((c for c in cols if c.lower() in ("day", "date", "month")), cols[0])
    sku = next((c for c in cols if "sku" in c.lower()), None)
    val = next((c for c in cols if "ending_inventory_units" in c.lower()
                or "inventory" in c.lower() or c not in (day, sku)), cols[-1])
    if not sku:
        raise RuntimeError(f"No SKU column in ShopifyQL result columns: {cols}")
    return pd.DataFrame({
        "day": pd.to_datetime(df[day], errors="coerce").dt.normalize(),
        "sku": df[sku].astype(str).str.strip(),
        "on_hand": pd.to_numeric(df[val], errors="coerce"),
    }).dropna(subset=["day", "sku"])


def _windows(since: date, until: date, step: int):
    cur = since
    while cur <= until:
        end = min(cur + timedelta(days=step - 1), until)
        yield cur, end
        cur = end + timedelta(days=1)


def backfill(shop: str, token: str, since: date, until: date, window: int = 4,
             out: str = DEFAULT_OUT, pause: float = 0.4) -> pd.DataFrame:
    session = requests.Session()
    parts: list[pd.DataFrame] = []
    for w0, w1 in _windows(since, until, window):
        win, ok = window, False
        # shrink the window if a response hits the row cap (would truncate)
        while not ok:
            frames = []
            for s0, s1 in _windows(w0, w1, win):
                df = run_shopifyql(QUERY.format(since=s0, until=s1), shop, token, session)
                if len(df) >= ROW_CAP and win > 1:
                    win = max(1, win // 2)
                    print(f"  cap hit {s0}..{s1} ({len(df)} rows) → shrink to {win}d", file=sys.stderr)
                    frames = None
                    break
                if len(df) >= ROW_CAP:  # already a 1-day window: can't shrink further
                    print(f"  ⚠️ {s0}: {len(df)} rows ≥ cap {ROW_CAP}; day may be truncated "
                          f"(store has >{ROW_CAP} SKUs)", file=sys.stderr)
                frames.append(df)
                time.sleep(pause)
            ok = frames is not None
        got = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        parts.append(got)
        print(f"{w0}..{w1}: {len(got):,} rows", file=sys.stderr)
    all_rows = pd.concat([p for p in parts if not p.empty], ignore_index=True) \
        if any(not p.empty for p in parts) else pd.DataFrame(columns=["day", "sku", "on_hand"])
    if all_rows.empty:
        print("No rows returned.", file=sys.stderr)
        return all_rows
    seed = (all_rows.drop_duplicates(["day", "sku"], keep="last")
            .rename(columns={"day": "date"}).sort_values(["sku", "date"]))
    seed["on_hand"] = seed["on_hand"].round().astype("Int64")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    seed[["date", "sku", "on_hand"]].to_csv(out, index=False)
    print(f"wrote {out}: {len(seed):,} rows · {seed['sku'].nunique()} SKUs · "
          f"{seed['date'].min():%Y-%m-%d}..{seed['date'].max():%Y-%m-%d}", file=sys.stderr)
    return seed


def _self_test():
    # column/row parsing is tolerant to ShopifyQL's column ordering
    table = {"columns": [{"name": "day"}, {"name": "product_variant_sku"},
                         {"name": "ending_inventory_units"}],
             "rowData": [["2026-01-01", "A", "10"], ["2026-01-02", "A", "-3"],
                         ["2026-01-01", "B", "0"]]}
    df = _parse_table(table)
    assert list(df.columns) == ["day", "sku", "on_hand"], df.columns.tolist()
    assert df["on_hand"].tolist() == [10, -3, 0], df["on_hand"].tolist()
    assert df["day"].dtype.kind == "M"
    assert len(list(_windows(date(2026, 1, 1), date(2026, 1, 10), 4))) == 3
    assert _endpoint("vegavero").endswith("/vegavero.myshopify.com/admin/api/%s/graphql.json" % API_VERSION)
    print("refresh_stock_history self-test OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2025-09-01", help="first day (YYYY-MM-DD)")
    ap.add_argument("--until", default=date.today().isoformat(), help="last day (YYYY-MM-DD)")
    ap.add_argument("--window", type=int, default=4, help="days per query (auto-shrinks on cap)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        _self_test()
        sys.exit(0)
    shop, token = os.environ.get("SHOPIFY_SHOP"), os.environ.get("SHOPIFY_ADMIN_TOKEN")
    if not shop or not token:
        sys.exit("Set SHOPIFY_SHOP and SHOPIFY_ADMIN_TOKEN (Admin API token, read_reports scope).")
    backfill(shop, token, date.fromisoformat(a.since), date.fromisoformat(a.until),
             window=a.window, out=a.out)

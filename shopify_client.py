"""Shopify order loader.

Two modes, chosen automatically:

* **Live** — when ``SHOPIFY_SHOP`` and ``SHOPIFY_ACCESS_TOKEN`` are present
  (env vars or Streamlit secrets), pulls orders straight from the Shopify
  Admin GraphQL API, paging through the whole date window. This is the "live
  via Shopify" path; the Admin API exposes every field the dashboard needs
  (line items, variant unitCost = COGS, refunds, payment gateway).

* **Sample** — otherwise reads ``data/sample_orders.json`` (a real pull
  committed to the repo) so the dashboard runs end-to-end with no credentials.

The returned object is always a list of order "nodes" in the same shape, so
margin.flatten_orders() doesn't care which mode produced it.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import requests

API_VERSION = "2025-01"
SAMPLE_PATH = Path(__file__).with_name("data") / "sample_orders.json"

ORDERS_QUERY = """
query($cursor: String, $q: String!) {
  orders(first: 100, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name createdAt displayFinancialStatus paymentGatewayNames
      totalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      lineItems(first: 50) {
        nodes {
          sku title quantity
          discountedTotalSet { shopMoney { amount } }
          product { id }
          variant { inventoryItem { unitCost { amount } measurement { weight { value unit } } } }
        }
      }
      refunds(first: 20) {
        refundLineItems(first: 50) {
          nodes { quantity lineItem { sku } subtotalSet { shopMoney { amount } } }
        }
      }
    }
  }
}
"""


def _credentials(secrets: dict | None = None) -> tuple[str | None, str | None]:
    shop = (secrets or {}).get("SHOPIFY_SHOP") if secrets else None
    token = (secrets or {}).get("SHOPIFY_ACCESS_TOKEN") if secrets else None
    shop = shop or os.environ.get("SHOPIFY_SHOP")
    token = token or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    return shop, token


def is_live(secrets: dict | None = None) -> bool:
    shop, token = _credentials(secrets)
    return bool(shop and token)


def _date_query(since: date, until: date) -> str:
    return f"created_at:>='{since.isoformat()}' created_at:<='{until.isoformat()}'"


def _fetch_live(shop: str, token: str, since: date, until: date) -> list[dict]:
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    q = _date_query(since, until)
    cursor, nodes = None, []
    while True:
        resp = requests.post(
            url, headers=headers,
            json={"query": ORDERS_QUERY, "variables": {"cursor": cursor, "q": q}},
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"Shopify GraphQL error: {payload['errors']}")
        block = payload["data"]["orders"]
        nodes.extend(block["nodes"])
        if block["pageInfo"]["hasNextPage"]:
            cursor = block["pageInfo"]["endCursor"]
        else:
            break
    return nodes


def _load_sample(since: date, until: date) -> list[dict]:
    if not SAMPLE_PATH.exists():
        return []
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        nodes = json.load(fh)
    # filter to the requested window on createdAt
    out = []
    for n in nodes:
        c = (n.get("createdAt") or "")[:10]
        if c and since.isoformat() <= c <= until.isoformat():
            out.append(n)
    return out or nodes  # if the window misses the sample entirely, show it all


def load_orders(since: date, until: date, secrets: dict | None = None) -> tuple[list[dict], str]:
    """Return (order_nodes, mode) where mode is 'live' or 'sample'."""
    shop, token = _credentials(secrets)
    if shop and token:
        return _fetch_live(shop, token, since, until), "live"
    return _load_sample(since, until), "sample"

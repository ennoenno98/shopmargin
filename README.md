# Shopify Product Margin Dashboard (CM1 / CM2 / CM3)

Interactive Streamlit dashboard showing contribution margin per product for the
Vegavero Shopify store, modelled on the existing Amazon **Margin Analytics**
workbook so the CM definitions stay consistent across channels.

```
CM1 = Net revenue − COGS
CM2 = CM1 − logistics/3PL − payment fees − packaging
CM3 = CM2 − allocated marketing / ad spend
```

(Equivalent to the Amazon file's GP1 / GP2 / Channel-margin ladder.)

## Run it

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
# → http://localhost:8501
```

With no credentials it runs in **sample mode** against a committed real pull
(`data/sample_orders.json`, May 2026). For **live** data, add Shopify Admin API
credentials in `.streamlit/secrets.toml` (or env vars):

```toml
SHOPIFY_SHOP = "your-store.myshopify.com"
SHOPIFY_ACCESS_TOKEN = "shpat_…"   # read scopes: orders, products, inventory
```

The app pages through the Admin GraphQL API for the selected date range and
caches the pull (TTL / manual **Refresh** button). Every field the CM model
needs is live: line-item revenue & discounts, `variant.inventoryItem.unitCost`
(COGS), refunds, and the payment gateway.

> Note: a deployed Streamlit app can't use the Claude/MCP Shopify connector —
> that's an authoring-time tool. Live mode therefore uses the Admin API
> directly (same data, same store).

## Where the numbers come from

| Layer | Source |
|------|--------|
| Revenue, units, discounts, refunds | Shopify orders (live) / sample |
| COGS | Shopify `unitCost` per variant (live) |
| Logistics / 3PL | **config** — €4.84/order (AP26 plan; reconciles with Klar actuals) |
| Payment fees | **config** — Shopify Payments 1.6%, PayPal 2.98%, per order gateway |
| Packaging | **config** — disabled by default |
| Marketing (CM3) | Klar Marketing Overview export → `data/klar_marketing.csv` |

All cost assumptions live in **`config.yaml`** and are overridable from the
sidebar (sidebar wins at runtime). Calculation logic is isolated in
`margin.py` and contains no hard-coded rates.

## Layout

```
streamlit_app.py        # UI (presentation only)
margin.py               # pure CM1/CM2/CM3 engine (testable, no Streamlit)
marketing.py            # Klar marketing ingestion & monthly aggregation
shopify_client.py       # live Admin API pull + sample fallback
config.py / config.yaml # cost assumptions + sidebar override merge
fetch_klar_export.py    # automated Klar download (see below)
data/                   # sample_orders.json, klar_marketing.csv
marketing/              # spend file + docs
.github/workflows/refresh_klar.yml  # daily Klar refresh
```

## Automating the Klar marketing pull

Set the repo secret `KLAR_MARKETING_EXPORT_URL` to a stable Klar export link
(see `fetch_klar_export.py` for ways to get one). The daily Action
`refresh_klar.yml` downloads, normalises, and commits
`data/klar_marketing.csv` — no more manual uploads.

## Consistency with Klar

Validated against the Klar profitability export: blended **CM1 ≈ 67%**
(Klar 66.3%). CM2 sits a little higher than Klar because we use the AP26 plan
logistics rate (€4.84 vs Klar's €5.43 actual) and flat payment-fee rates, per
the agreed cost model.

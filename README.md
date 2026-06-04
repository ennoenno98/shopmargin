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

**Data source: Matrixify exports — no Shopify API token needed.** The dashboard
reads two Matrixify exports:

- `data/matrixify_orders.csv` — Orders (with line items, discounts, refunds, transaction gateway)
- `data/matrixify_products.csv` — Products (with **Variant Cost** = COGS, weight)

If those files aren't present it falls back to a committed JSON **sample**
(`data/sample_orders.json`) so the demo runs with zero setup.

### Matrixify export setup
Create two scheduled exports in Matrixify and have them upload to a cloud
destination (S3 / Google Drive / Dropbox / FTP):

- **Products** — columns: `Variant SKU`, `Variant Cost`, `Variant Price`, `Variant Grams`, `Title`, `Status`, `Vendor`.
- **Orders** — columns: `Name`, `Created At`, `Financial Status`, `Currency`, `Line: SKU`, `Line: Quantity`, `Line: Price`, `Line: Discount`, `Line: Total`, refund columns, `Transaction: Gateway`, `Transaction: Amount`.

Then set repo secrets `MATRIXIFY_ORDERS_URL` and `MATRIXIFY_PRODUCTS_URL` to
the upload links; the daily Action `refresh_matrixify.yml` downloads (unzips if
needed) and commits the refreshed CSVs. Column names are matched tolerantly, so
minor Matrixify naming differences are fine.

## Where the numbers come from

| Layer | Source |
|------|--------|
| Revenue, units, discounts, refunds | Matrixify **Orders** export |
| COGS | Matrixify **Products** export (`Variant Cost`) |
| Logistics / 3PL | **config** — €4.84/order (AP26 plan; reconciles with Klar actuals) |
| Payment fees | **config** — Shopify Payments 1.6%, PayPal 2.98%, per order gateway |
| Packaging | **config** — disabled by default |
| Marketing (CM3) | Klar Marketing Overview export → `data/klar_marketing.csv` |

All cost assumptions live in **`config.yaml`** and are overridable from the
sidebar (sidebar wins at runtime). Calculation logic is isolated in
`margin.py` and contains no hard-coded rates.

## Layout

```
streamlit_app.py         # UI (presentation only)
margin.py                # pure CM1/CM2/CM3 engine (testable, no Streamlit)
matrixify_client.py      # reads Matrixify Orders + Products exports
data_source.py           # picks Matrixify files, else the JSON sample
marketing.py             # Klar marketing ingestion & monthly aggregation
config.py / config.yaml  # cost assumptions + sidebar override merge
fetch_matrixify_export.py / fetch_klar_export.py   # automated downloads
data/                    # matrixify_*.csv, klar_marketing.csv, sample_orders.json
.github/workflows/refresh_matrixify.yml + refresh_klar.yml   # daily refresh
```

## Automating the data pulls

- **Shopify data (Matrixify):** repo secrets `MATRIXIFY_ORDERS_URL` +
  `MATRIXIFY_PRODUCTS_URL` → daily `refresh_matrixify.yml`.
- **Marketing (Klar):** repo secret `KLAR_MARKETING_EXPORT_URL` → daily
  `refresh_klar.yml` (see `fetch_klar_export.py` for how to get a stable link).

Both commit refreshed CSVs to `data/`; the dashboard reads whatever is latest.

## Consistency with Klar

Validated against the Klar profitability export: blended **CM1 ≈ 67%**
(Klar 66.3%). CM2 sits a little higher than Klar because we use the AP26 plan
logistics rate (€4.84 vs Klar's €5.43 actual) and flat payment-fee rates, per
the agreed cost model.

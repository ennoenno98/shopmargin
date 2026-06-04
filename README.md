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

### Matrixify refresh — via the Matrixify MCP server (no Drive/FTP)

The data is refreshed straight from the **Matrixify MCP server**
(`https://mcp.matrixify.app/mcp`) by `refresh_matrixify_mcp.py`, run daily by
the `refresh_matrixify.yml` GitHub Action. It creates the exports, downloads
them, and commits — no cloud-storage hop and no manual links.

**The 10k-orders-per-job cap:** a single Matrixify export is capped at the
plan's order limit (Big plan = 10,000 orders/job), so one unfiltered export of a
~17k-order store silently drops the overflow. The script avoids this by
exporting orders in **monthly `created_at` chunks** and stitching them; if any
month ever nears the cap the chunk is split in half and retried, so the result
is always complete. Products (incl. **Variant Cost** = COGS) come from a single
export. Column names are matched tolerantly downstream in `matrixify_client.py`.

**One-time setup:** in the Matrixify app, go to **Settings → AI Agent MCP
Tokens → Generate Token**, then add it as the repo secret
**`MATRIXIFY_MCP_TOKEN`**. (Optional repo *variable* `ORDERS_SINCE`, default
`2025-07-01`, sets the first order month.) That's it — the daily Action does the
rest. Run it on demand from the Actions tab ("Run workflow") or locally:

```bash
pip install -r requirements-export.txt
MATRIXIFY_MCP_TOKEN=… python refresh_matrixify_mcp.py
python refresh_matrixify_mcp.py --self-test   # offline: chunking + stitch checks
```

> Note: this must run somewhere with open network (CI / your machine). A Claude
> Code **web** session can drive the MCP but its egress allowlist blocks
> `app.matrixify.app`, so it can't download the export files itself.

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
refresh_matrixify_mcp.py # refresh orders+products via the Matrixify MCP (chunked, stitched)
fetch_matrixify_export.py / fetch_klar_export.py   # legacy URL fetch (Matrixify) / Klar download
data/                    # matrixify_*.csv, klar_marketing.csv, sample_orders.json
.github/workflows/refresh_matrixify.yml + refresh_klar.yml   # daily refresh
```

## Automating the data pulls

- **Shopify data (Matrixify):** repo secret `MATRIXIFY_MCP_TOKEN` → daily
  `refresh_matrixify.yml` (runs `refresh_matrixify_mcp.py` against the Matrixify
  MCP server; chunks orders to beat the 10k/job cap, then stitches + commits).
- **Marketing (Klar):** repo secret `KLAR_MARKETING_EXPORT_URL` → daily
  `refresh_klar.yml` (see `fetch_klar_export.py` for how to get a stable link).

Both commit refreshed CSVs to `data/`; the dashboard reads whatever is latest.

## Consistency with Klar

Validated against the Klar profitability export: blended **CM1 ≈ 67%**
(Klar 66.3%). CM2 sits a little higher than Klar because we use the AP26 plan
logistics rate (€4.84 vs Klar's €5.43 actual) and flat payment-fee rates, per
the agreed cost model.

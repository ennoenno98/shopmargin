# Marketing spend (CM3)

CM3 = CM2 − allocated marketing. Spend comes from the **Klar Marketing
Overview** export, stored as `../data/klar_marketing.csv`.

## Format
Channel-level, one row per channel × month:

| channel | month | cost | channel_net_revenue | roas | orders |
|---------|-------|------|---------------------|------|--------|
| Google Generic Paid Search | 2026-05-01 | 2491.73 | 5447.72 | 2.3 | 130 |

Only `cost`, `month`, and `channel_net_revenue` are required by the engine.

## How it maps to products
For each calendar month, the **total** marketing `cost` is allocated across
products **pro-rata by net revenue** (configurable to units in the sidebar).
The denominator is Klar's **full-month** store net revenue (sum of
`channel_net_revenue`), so the allocation stays correct even when the
dashboard window covers only part of a month.

## Keeping it fresh
- **Manual:** export "Marketing Overview" from Klar and run
  `python ../fetch_klar_export.py` (or just drop a new CSV here).
- **Automated:** set the repo secret `KLAR_MARKETING_EXPORT_URL` to a stable
  Klar export link; the daily GitHub Action `refresh_klar.yml` re-pulls and
  commits it. See `../fetch_klar_export.py` for how to obtain a stable URL.

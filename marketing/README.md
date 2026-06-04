# Marketing spend (CM3)

CM3 = CM2 − allocated marketing. Spend comes from the **Klar Marketing
Overview** export, stored as `../data/klar_marketing.csv`.

## Format
Klar's "Marketing Overview" export, **monthly or daily** — both are handled
(daily `Date` rows are bucketed to month). The reader is column-tolerant, so it
accepts the raw Klar headers (`Channel Name`, `Date`/`Calendar Month`, `Cost`,
`Revenue KPIs Net Revenue`). The committed `data/klar_marketing.csv` is the
compact channel × month form:

| channel | month | cost | channel_net_revenue |
|---------|-------|------|---------------------|
| Google Generic Paid Search | 2025-10-01 | 2491.73 | 5447.72 |

Only `cost`, `month`, and `channel_net_revenue` are required by the engine.
"Totals" rows are dropped automatically to avoid double-counting.

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

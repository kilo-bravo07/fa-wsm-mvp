# Source extracts

`src/build_dataset.py` expects these five files. Filenames are fixed; re-export
in place and re-run the build to refresh the model.

| File | Source | Feeds | Required columns |
|---|---|---|---|
| `products.csv` | CMS / internal DB export of `/all-flowers` | catalogue, M6 | `GUID`, `Title`, `Sku`, `url`, `Sell Price`, `List price`, `Bouquet Type`, `Page display`, `Create Date and Time`, `Best Seller` |
| `plp_metrics.csv` | GA4 / BigQuery, item-scoped to the `/all-flowers` list | M1, M2 | `Item ID`, `Item name`, `PLP Views`, `PLP Clicks`, `PLP ATC` |
| `pdp_metrics.csv` | GA4 / BigQuery, product detail page | M4 | `Item name`, `Item category`, `PDP Views`, `PDP ATC`, `PDP Purchase` |
| `sales.csv` | Internal DB order lines | M3, M5 | `order_date`, `order_id`, `nid`, `qty`, `order_total` |
| `current_sequence.csv` | Live page order | control | `Product Name`, `Index` |

## Join keys

- `plp_metrics.csv` joins on `Item ID` = `GUID`. Verified 240/240.
- `pdp_metrics.csv` carries the **whole catalogue** (~12.9k rows, 20 categories) and
  has no ID column. The build filters to `Item category = flower` and matches on
  name. Eight names in that file are duplicated across categories; the build aborts
  if any collide with our SKUs.
- `current_sequence.csv` has no ID column and matches on name.

Both name-based joins are MVP conveniences. Add `GUID` to these extracts when you
can — two products differ only by an apostrophe (`Fathers Day Special Box` /
`Father's Day Special Box`).

## Window

The analysis window is set in `src/build_dataset.py` (`WINDOW_START` / `WINDOW_END`).
Sales are sliced to it; the GA4 extracts are assumed to already match it. Recency
(M5) deliberately uses the **full** sales history rather than the window — a product
last sold in April is stale regardless of which window is being scored.

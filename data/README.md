# Source extracts

`src/build_dataset.py` expects these nine files. Filenames are fixed; re-export in
place and re-run the build.

## Flowers — `/all-flowers`

| File | Source | Required columns |
|---|---|---|
| `products.csv` | CMS export | `GUID`, `Title`, `Sku`, `url`, `Sell Price`, `Page display`, `Create Date and Time` |
| `plp_metrics.csv` | GA4, scoped to the /all-flowers list | `Item ID`, `PLP Views`, `PLP Clicks`, `PLP ATC` |
| `pdp_metrics.csv` | GA4, product detail page | `Item name`, `Item category`, `PDP Views`, `PDP ATC` |
| `sales.csv` | Internal DB order lines | `order_date`, `order_id`, `nid`, `order_total` |
| `current_sequence.csv` | Live page order | `Product Name`, `Index` |

## Combos — `/combos/flowers-and-cakes`

| File | Required columns |
|---|---|
| `plp_combos.csv` | `Item ID`, `Item name`, `PLP Views`, `PLP Clicks`, `PLP ATC` |
| `pdp_combos.csv` | `Item name`, `PDP Views`, `PDP ATC`, `PDP Purchase` |

## Hampers — `/hampers/same-day-delivery`

| File | Required columns |
|---|---|
| `plp_hampers.csv` | `Item ID`, `Item name`, `PLP Views`, `PLP Clicks`, `PLP ATC` |
| `pdp_hampers.csv` | `Item name`, `PDP Views`, `PDP ATC`, `PDP Purchase` |

## Known quirks in the current extracts

**Every row repeats, several times.** The duplicates are identical, so the build drops
them. If a future export produces *conflicting* values for one key, the build will pick
one arbitrarily — worth fixing at source.

**Some rows have no `Item ID`.** Four combos and three hampers. They cannot be joined to
anything and are dropped with a notice naming them.

**PDP extracts carry no `Item ID`.** They are matched on product name. The build aborts
if a name collides within the relevant category rather than letting the join fan out.

**`PDP Purchase` is the only order source for combos and hampers.** It is not capturing
combo purchases — see the README. Hamper figures look sound.

**No price, launch date or stock status** for combos and hampers. No stock status for
flowers either.

## Window

Flower orders are sliced to `window.days` ending `window.flowers_orders_end` in
`config/config.json`. The GA4 extracts for all three pools arrived without a stated
range and are assumed to match. Re-pull everything on one declared window before
quoting figures externally.

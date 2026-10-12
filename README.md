# Listing Page Sequencing — FlowerAura `/all-flowers`

An MVP of the listing-page sequencing algorithm: products are scored on four
behavioural signals, then dealt into a repeating ten-slot template that holds a price
mix, a new-arrival seat and a featured seat. Every weight, band and slot is editable in
the browser so a reviewer can see the page re-sequence live.

**[Open the viewer →](https://kilo-bravo07.github.io/fa-wsm-mvp/)**

The earlier Weighted Sequence Model port (the Bakingo six-metric design) is kept at
[`docs/wsm-v1.html`](docs/wsm-v1.html) for reference. This page supersedes it.

---

## Step 1 — Score

| Signal | Definition | Weight |
|---|---|---|
| Click-through rate | PLP clicks ÷ PLP impressions | 35% |
| Add-to-cart PDP rate | PDP add-to-cart ÷ PDP views | 25% |
| Conversion rate (SKU) | orders ÷ PLP clicks | 20% |
| Overall SKU orders | order count | 20% |

Each signal is normalised 0–1 across the page before weighting. Rates are smoothed
toward the site average first.

## Step 2 — Slots

The page is dealt in batches of ten. Each position asks for a slot type, and the type
takes the highest-scoring unplaced product from its pool.

| Slot | Rule |
|---|---|
| **M** Medium | price ₹750–1200, highest score |
| **H** High | price above ₹1200, highest score |
| **N** New arrival | launched in the last 30 days, highest score |
| **F** Featured | drawn from the manual boost list |

```
Batch 1    M M H M F M N M H M
Batch 2    M M H M F M N M H M
Batch 3+   M M H M F M N M M M
```

## Step 3 — The rest

The same template continues to the end of the catalogue. Out-of-stock products are
barred from the top 10 and sink to the page tail.

---

## The fallback chain

The PRD's slot rules don't produce enough products to fill the seats they ask for, so
every slot falls through an ordered chain when its own pool empties. Measured on the
current 240-SKU catalogue:

| Slot | Products available | Seats wanted | Short by |
|---|---|---|---|
| M (₹750–1200) | 56 | 166 | **110** |
| H (>₹1200) | 125 | 26 | — |
| N (last 30 days) | 12 | 24 | **12** |
| F (boost list) | 0 | 24 | **24** |
| LOW (<₹750) | 58 | 0 | — |

Two things to note. **58 products priced below ₹750 belong to no band the PRD names** —
without a fallback they could never be placed at all. And the strict ₹750–1200 M band
qualifies only 56 products against 166 M seats.

The chain (`M → LOW → H`, `H → M → LOW`, `N` and `F → M → LOW → H`) resolves both: the
strict band governs the top of the page, where the price mix actually matters, and every
product still lands exactly once. It is a config value, not a hard-coded rule.

## Two behaviours worth knowing

**Boosted products are reserved for F seats.** Without this, an M slot takes a boosted
SKU before its F seat comes round — the boost buys nothing and the F seat falls back to
an ordinary product. Set `rules.reserve_boost_for_f` to `false` to disable.

**The F pick is deterministic.** Requirement 6 asks for the slot to be stable within a
session, so the "random" draw from the boost list uses a fixed seed rather than
`Math.random()`.

---

## Repository layout

```
config/config.json       weights, bands, template, fallback chain, boost + manual-new lists
src/sequence.py          reference scorer and slot allocator — authoritative maths
src/build_dataset.py     joins the five extracts -> docs/data.{json,js}
docs/index.html          the viewer (GitHub Pages serves /docs)
docs/wsm-v1.html         the superseded Weighted Sequence Model viewer
tests/test_sequence.py   PRD requirement checks + Python/JavaScript parity
data/                    source extracts (see data/README.md)
```

## Running it

```bash
pip install -r requirements.txt
python src/build_dataset.py      # regenerates docs/data.json and docs/data.js
python -m pytest tests/ -q       # requirement checks + parity (needs node)
open docs/index.html             # works from the filesystem, no server needed
```

Publishing: **Settings → Pages → Deploy from branch → `main` / `/docs`**.

> `data/*.csv` is excluded from the repository — it contains order-level revenue data.
> The viewer runs entirely off `docs/`.

## Requirements coverage

| # | Requirement | Status |
|---|---|---|
| 1 | Score refreshes on a schedule | Config value; the MVP recomputes on load |
| 2 | A product appears only once | Tested |
| 3 | Out-of-stock never in the top 10 | Implemented, tested with synthetic stock data |
| 4 | Empty pool fills from elsewhere | Implemented as the fallback chain, tested |
| 5 | Boost list editable by the category team | Editable in the viewer and in `config.json` |
| 6 | F pick stable within a session | Deterministic seed |
| 7 | Empty boost list falls back | Tested |
| 8 | Template, bands and weights configurable | All live in the viewer |
| 9 | Served from cache, no slowdown | Out of scope for the MVP |

## Known gaps

**Stock status is not in any extract.** Every product is treated as in stock, so the
three stock rules are implemented and unit-tested against synthetic data but unverified
against production. A `GUID, in_stock` column activates them.

**The GA4 window is unconfirmed.** The PRD specifies a trailing 30 days. The sales data
is sliced to the last 30 days available (24 Aug – 22 Sep 2026), but the PLP and PDP
extracts arrived without a stated range and are a longer aggregate — so CTR and PDP
rates read high relative to a true 30-day pull. Re-pull both at 30 days before quoting
figures externally.

**New arrivals run out after a dozen products**, nearly all one Korean range launched on
a single day, against 24 N seats. Use the manual new-arrival list to seed the pool, or
accept that N stops appearing partway down the page.

**Three of the four signals share a funnel.** PDP add-to-cart rate, conversion rate and
order count all measure stages of the same journey, so 65% of the weight sits downstream
of the click and the three will move together. The effective weighting is more
concentrated than 35/25/20/20 reads.

## Open questions from the PRD

1. Should products undeliverable to the visitor's pincode be pushed down and greyed out?
   Not modelled — needs serviceability data per SKU.
2. Who owns the boost list, and does each boost carry an end date? The config has no
   expiry field yet; say the word and I'll add one.
3. Should birthday, anniversary and other pages get their own boost lists? The config is
   per-page already, so this is a deployment question rather than a code change.

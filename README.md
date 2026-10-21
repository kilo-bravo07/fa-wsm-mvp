# Listing Page Sequencing — FlowerAura `/all-flowers`

Products are scored on four behavioural signals, then dealt into a repeating thirty-slot
template holding five price bands plus combos and hampers. Boosted SKUs are inserted on
top, outside the thirty. Every weight, slot and insertion is editable in the browser.

**[Open the viewer →](https://kilo-bravo07.github.io/fa-wsm-mvp/)**

Earlier versions are kept for reference: [`docs/wsm-v1.html`](docs/wsm-v1.html) is the
original Bakingo six-metric port.

---

## Step 1 — Score

| Signal | Definition | Weight |
|---|---|---|
| Click-through rate | clicks ÷ impressions | 35% |
| ATC rate | PDP add-to-carts ÷ clicks | 25% |
| Conversion rate | orders ÷ clicks | 20% |
| Overall SKU orders | order count | 20% |

Each is normalised 0–1 across the catalogue before weighting. Rates are smoothed toward
the site average first, and a product below the impression floor takes the median score of
its own bucket rather than being scored on noise.

## Step 2 — The thirty-slot template

```
L  M  C  L  U  P  M  L  H  C
U  L  P  M  V  L  C  U  P  M
L  H  C  U  P  M  V  L  C  P
```

| Slot | Bucket | Count |
|---|---|---|
| L | Under ₹1,000 | 7 |
| M | ₹1,000–1,499 | 5 |
| U | ₹1,500–1,999 | 4 |
| H | ₹2,000–2,499 | 2 |
| V | Above ₹2,500 | 2 |
| C | Combo | 5 |
| P | Hamper | 5 |

Each slot takes the highest-scoring unplaced product from its bucket. A product is claimed
by the first bucket in `precedence` it qualifies for — combo and hamper outrank price bands
— which is what guarantees one SKU, one placement. The template repeats to the end of the
catalogue.

## Step 3 — Boost insertions

Up to four per set. The template deals the whole catalogue, so a boosted SKU already sits
somewhere on the page; a boost **lifts it from that position and re-inserts it** at the
configured one. The set then shows 30+x while every SKU still appears exactly once, and the
products it jumps keep their relative order.

---

## The catalogue

362 products across three pools with zero overlap:

| Pool | Count | Metrics measured on | Price | Launch date |
|---|---:|---|---|---|
| Flower | 240 | `/all-flowers` | yes | yes |
| Hamper | 62 | `/hampers/same-day-delivery` | no | no |
| Combo | 60 | `/combos/flowers-and-cakes` | no | no |

Combos and hampers go to their own buckets regardless of price, so the missing prices do not
block the build — but combo-versus-price-band precedence cannot be verified without them.

---

## Three data problems, all visible on the page

**Combo orders are not being captured.** Only **3 of 60 combos** report a purchase in the PDP
extract, yet 35 of them sold in the earlier order file — `Mystic Orchids N Cake Combo` at 156
orders and `Red Rose N Truffle` at 120 both read zero here. Two of the four signals are
order-derived, so 40% of the combo score is currently zero for almost the whole bucket and the
Combo slots are decided on CTR and ATC alone. `Make Your Own Combo` takes the top combo slot on
315 purchases against 1 add-to-cart, which is not credible either.

**The "÷ clicks" denominators do not work on this data.** It is what the PRD specifies and what
the page loads with, but add-to-carts and orders arrive through routes that never touched the
listing while clicks are counted only on it. Measured: **213 of 346 products exceed 100%** on
ATC rate, median 132%, maximum 1,000%. Switched to PDP views, nothing exceeds 100% and the
median falls to 9.2%. The switch changes **14 of the top 30**, so it is not cosmetic. Both
denominators are dropdowns in the viewer.

**Three pools, three pages.** A product's CTR reflects whichever listing it was measured on.
Buckets contain this — combos only compete with combos — but the fallback chain crosses buckets.

Also still missing: stock status (so the two out-of-stock rules are implemented but unverified)
and a confirmed date window for any of the GA4 extracts.

---

## Bucket depth

The page runs 13 sets. How many each bucket can cover before its slot falls back:

| Bucket | Products | Slots/set | Full sets |
|---|---:|---:|---:|
| V Above ₹2,500 | 40 | 2 | 20 |
| L Under ₹1,000 | 95 | 7 | 13 |
| C Combo | 60 | 5 | 12 |
| P Hamper | 62 | 5 | 12 |
| H ₹2,000–2,499 | 21 | 2 | 10 |
| M ₹1,000–1,499 | 48 | 5 | 9 |
| U ₹1,500–1,999 | 35 | 4 | 8 |

The intended mix holds firmly through the first eight sets and loosens below, which is the
right shape — accuracy where the traffic is.

## Fallback

The PRD says an empty pool borrows the equivalent SKU from a later batch. That cannot
terminate once a pool is globally exhausted, and pulling a product forward leaves a hole that
cascades to the end of the page. Instead a price slot falls through to the **nearest band by
price**, then to the best remaining product of any bucket; combo and hamper slots fall straight
through to best remaining. It is a config value, not a hard-coded rule.

---

## Repository layout

```
config/config.json       weights, buckets, template, precedence, fallback, boosts
src/sequence.py          reference scorer and allocator — authoritative maths
src/build_dataset.py     merges the three pools -> docs/data.{json,js}
docs/index.html          the viewer (GitHub Pages serves /docs)
tests/test_sequence.py   PRD requirement checks + Python/JavaScript parity
data/                    source extracts (see data/README.md)
```

## Running it

```bash
pip install -r requirements.txt
python src/build_dataset.py      # regenerates docs/data.json and docs/data.js
python -m pytest tests/ -q       # 26 checks (needs node for the parity tests)
open docs/index.html             # works from the filesystem, no server needed
```

Publishing: **Settings → Pages → Deploy from branch → `main` / `/docs`**.

> `data/*.csv` is excluded from the repository — it contains order-level revenue data.
> The viewer runs entirely off `docs/`.

## Requirements coverage

| # | Requirement | Status |
|---|---|---|
| 1 | Scores refresh every 7 days, configurable | Config value; the MVP recomputes on load |
| 2 | A product appears only once | Tested, including under boosts |
| 3 | Out-of-stock never in the top 10 | Implemented, tested against synthetic stock |
| 4 | Empty slot pool is filled | Fallback chain, tested |
| 5 | Boost list editable by the category team | Editable in the viewer and in `config.json` |
| 6 | F pick stable within a session | Deterministic — position-driven, no randomness |
| 7 | Empty boost list falls back | Tested |
| 8 | Template, bands and weights configurable | All live in the viewer |
| 9 | Served from cache, no slowdown | Out of scope for the MVP |

## Exports

**Sequence** — `nid|product name|index`, pipe-delimited, 1-based, always in page order.
**Full CSV** — every column including bucket, slot, fallback source and score.

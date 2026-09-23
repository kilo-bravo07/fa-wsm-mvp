# Weighted Sequence Model — FlowerAura `/all-flowers`

An MVP of the Weighted Sequence Model (WSM), ported from Bakingo to the FlowerAura
flowers listing page. It replaces static product sorting with a score computed from
behavioural and sales data, and lets a reviewer move the weights and watch the
sequence respond.

**[Open the viewer →](https://YOUR-ORG.github.io/fa-wsm-mvp/)**
*(replace with the Pages URL once published)*

---

## What the model does

Every product gets a Rank Score. Higher scores rank earlier on the page.

```
S1 = M1·A + M2·B + M3·C + M4·D
S2 = S1 · E      if the recency condition holds
RS = S2 · F      if the price condition holds
```

| | Metric | Definition | Default weight | Source |
|---|---|---|---|---|
| M1 | PLP click-through rate | PLP clicks ÷ PLP views | A = 45% | GA4 / BigQuery |
| M2 | PLP add-to-cart rate | PLP ATC ÷ PLP clicks | B = 25% | GA4 / BigQuery |
| M3 | Order share | SKU orders ÷ all flower orders | C = 20% | Internal DB |
| M4 | PDP add-to-cart rate | PDP ATC ÷ PDP views | D = 10% | GA4 / BigQuery |
| M5 | Recency modifier | ordered within the recency window | E = ×1.40 | Internal DB |
| M6 | Price modifier | price vs. peer-group median | F = ×1.40 | Internal DB |

## What the data says

Measured over 240 products, 4.06M PLP impressions and 52,802 attributed orders.

**The current sequence is uncorrelated with performance.** Position predicts
impressions strongly (Spearman ρ = −0.41) but has no significant relationship with
click-through rate (ρ = 0.07, p = 0.31). Products are ordered, but not by anything
that predicts whether a customer wants them.

**36% of the page's impressions return 4% of its orders.** Eighteen products draw
more than 30,000 impressions each while converting below half the page average.
`Harmonious Veil Of Dried Blooms` needs 7,189 impressions to produce one order
against a page average of 77. Meanwhile `Blossoming Whispers Of Love` needs 5 — and
sits at position 202.

**Add-to-cart rate rises monotonically with depth**, from 13.2% in positions 1–20 to
23.7% in positions 101–150. The products that convert best once clicked are the ones
almost nobody reaches.

## Repository layout

```
src/build_dataset.py   joins the five source extracts -> docs/data.{json,js}
src/wsm.py             reference scorer; the authoritative definition of the maths
docs/index.html        the viewer (GitHub Pages serves this directory)
docs/data.js           generated model input, one record per SKU
tests/test_parity.py   join-integrity checks + Python/JavaScript scorer parity
data/                  source extracts (see data/README.md)
```

## Running it

```bash
pip install -r requirements.txt
python src/build_dataset.py      # regenerates docs/data.json and docs/data.js
python -m pytest tests/ -q       # join checks + scorer parity (needs node)
open docs/index.html             # works from the filesystem, no server needed
```

To publish: **Settings → Pages → Deploy from branch → `main` / `/docs`**.

> **Make the repository private.** `data/sales.csv` is 15 MB of order-level data
> (order IDs, dates, line prices, order totals — no customer PII, but full revenue
> detail) and `docs/data.js` carries per-product revenue. Neither belongs on a public
> URL. If stakeholders need a public link, delete `data/` from the repo and commit
> only `docs/` — the viewer runs entirely off `docs/data.js` and needs nothing else.

## Decisions this implementation makes

The spec left several things open. Each is implemented as a live control in the
viewer rather than a hard-coded choice, so the review can settle them.

**Normalisation.** M1–M4 are rates on wildly different scales — page CTR runs near
1.6%, ATC rate near 18%, order share near 0.09%. Summing them raw would let one
metric dominate through its units alone. Each is mapped to 0–1 across the page
before weighting. Min–max is the default; percentile rank is more robust when a
single outlier compresses everyone else.

**Smoothing.** A product with 5 clicks and 2 add-to-carts shows a 40% ATC rate.
Unsmoothed, it outranks genuinely strong products on noise alone. Each ratio is
shrunk toward the page's pooled rate in proportion to how little traffic supports
it. Set smoothing to Off in the viewer to see what the unguarded model would do.

**Cold start.** 50 of 240 products were created after the analysis window opened.
Scoring them at zero would bury them permanently — a self-fulfilling prophecy — so
they take the median score of established products until they accumulate history.

**M6 peer group.** The catalogue's `Category` field is `"Main"` for every row, so
bouquet type stands in. Groups below 10 products fall back to the page-wide
benchmark: in a 3-product group every member sits at its own median and the modifier
stops discriminating. The benchmark is the **median, not the mean** — one ₹45,045
product drags the Bunches mean to ₹2,395 against a median of ₹995, which would mark
70% of the group "below average" and flatten the signal.

## Known issues with the model as specified

**M5 does almost nothing.** At the specified three-month threshold, 237 of 240
products qualify as recent. Multiplying 98.7% of the catalogue by 1.4 changes no
relative ordering — it is arithmetically identical to having no M5 at all. Recency
was designed for a cake catalogue where long-tail SKUs go dormant; a live flower page
sells nearly everything nearly every day. The viewer exposes shorter windows; 7 or 14
days makes the modifier discriminate. Consider replacing the step function with
graded decay.

**M4 largely restates M2.** The two add-to-cart metrics correlate at ρ = 0.77 — they
measure much the same propensity at two different surfaces. The effective weighting
is therefore more concentrated on add-to-cart than 45/25/20/10 suggests. At D = 10%
the distortion is small, but the four metrics are not independent.

**M4's scope doesn't match M1 and M2.** M1 and M2 are scoped to this listing page,
but `view_item` is global — only about 10% of PDP traffic arrives from
`/all-flowers`. M4 therefore measures site-wide product strength, not this page's.
Inherited from the Bakingo definition and kept identical on purpose.

**E and F compound.** Both firing gives ×1.96, which can outweigh the entire M1–M4
spread. Worth watching when tuning.

**Large-format bouquets are demoted hard.** Every 100/250/365/1000-rose product
bottoms out on M4 — high-consideration items people look at and don't cart. Probably
correct for an evergreen sequence, but these are brand-halo products. A pinned-position
override in the config layer is the sane answer.

## Open questions

1. **GA4 date window.** The analytics extracts arrived without a stated range. The
   build assumes they align with the order window (25 Jun – 22 Sep 2026); the viewer
   flags this. Confirm and update `WINDOW_START` / `WINDOW_END` in
   `src/build_dataset.py`.
2. **M6 direction.** Currently boosts below-median prices (volume play). Inverting it
   boosts premium products (AOV play). Toggleable in the viewer.
3. **M3 source.** Built from internal DB orders. GA4 `PDP Purchase` is also available
   and consistent with M1/M2/M4, but undercounts through attribution loss.

## Production notes

This MVP joins PDP metrics and the current sequence **on product name**, because
neither extract carried an ID column. All 240 matched cleanly, but the production
Ranking Service must join on `GUID` — product titles are editable in the CMS, and two
products in this catalogue already differ only by an apostrophe.

Positions are not debiased. That is usually a serious omission, but here current
position is uncorrelated with CTR, so measured rates are not meaningfully contaminated
by placement. This will stop being true once WSM ships and starts concentrating
impressions on high performers — from that point, position-debiasing becomes required
before recomputing weights, or the model will compound its own first ranking.

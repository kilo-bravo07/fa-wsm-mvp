"""
FlowerAura Weighted Sequence Model (WSM) — dataset builder.

Joins the five source extracts into a single model-input file consumed by the
MVP viewer (docs/index.html).

Sources
-------
1. products      — Internal DB / CMS export of every SKU on /all-flowers
2. plp_metrics   — GA4/BigQuery, item-scoped to the /all-flowers list  -> M1, M2
3. pdp_metrics   — GA4/BigQuery, product detail page, all categories   -> M4
4. sales         — Internal DB order lines, 1 Mar - 22 Sep 2026        -> M3, M5
5. sequence      — Current live ordering of the page                   -> control

Output
------
docs/data.json — one record per SKU with RAW metric inputs only. All
normalisation, smoothing and weighting happens client-side so the stakeholder
can move the weights and watch the ranking respond.

Usage
-----
    python src/build_dataset.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "data.json"
# Also emitted as a plain script so the viewer opens from the filesystem, not
# only from a web server — reviewers who clone the repo can double-click it.
OUT_JS = ROOT / "docs" / "data.js"

# --- Analysis window -------------------------------------------------------
# The GA4 extracts arrived without a stated date range. We assume they align to
# the trailing 90 days and slice the sales data to match. If the GA4 window is
# confirmed to be different, change these two dates and re-run.
WINDOW_START = pd.Timestamp("2026-06-25")
WINDOW_END = pd.Timestamp("2026-09-22")
AS_OF = pd.Timestamp("2026-09-23")

# Peer groups smaller than this fall back to the page-wide benchmark for M6.
# With a 3-SKU peer group every member sits within a rupee or two of its own
# group median and the modifier stops discriminating entirely.
MIN_PEER_GROUP = 10


def _read(name: str, **kw) -> pd.DataFrame:
    path = DATA / name
    if not path.exists():
        sys.exit(f"Missing source file: {path}\nSee data/README.md for the expected extracts.")
    if path.suffix == ".xlsx":
        return pd.read_excel(path, **kw)
    return pd.read_csv(path, **kw)


def norm_name(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower()


def primary_bouquet_type(value) -> str:
    """`Bouquet Type` is multi-valued ("Bunches, Premium"). Take the first token."""
    if pd.isna(value):
        return "Unclassified"
    return str(value).split(",")[0].strip() or "Unclassified"


def build() -> dict:
    products = _read("products.csv")
    plp = _read("plp_metrics.csv")
    pdp = _read("pdp_metrics.csv")
    sales = _read("sales.csv")
    sequence = _read("current_sequence.csv")

    # ---- products ---------------------------------------------------------
    products = products.rename(columns={"GUID": "guid", "Title": "title", "Sku": "sku"})
    products["created"] = pd.to_datetime(
        products["Create Date and Time"].astype(str).str.split(", ").str[-1],
        format="%m/%d/%Y - %H:%M",
        errors="coerce",
    )
    products["peer_raw"] = products["Bouquet Type"].map(primary_bouquet_type)

    # `Page display` is null for most rows; only an explicit "Donot display"
    # takes a product off the page. Corroborated by GA4: the single
    # "Donot display" SKU is also the only one with zero impressions.
    products["on_page"] = products["Page display"].fillna("Display").str.strip() != "Donot display"

    # Peer groups below the floor are pooled so M6 keeps a meaningful benchmark.
    counts = products["peer_raw"].value_counts()
    small = set(counts[counts < MIN_PEER_GROUP].index)
    products["peer_group"] = products["peer_raw"].where(~products["peer_raw"].isin(small), "Other")

    df = products[
        ["guid", "title", "sku", "url", "Sell Price", "List price", "peer_raw", "peer_group",
         "created", "on_page", "Best Seller"]
    ].rename(columns={"Sell Price": "price", "List price": "mrp", "Best Seller": "best_seller"})

    # ---- M1, M2: PLP metrics (join on GUID — verified 240/240) ------------
    plp = plp.rename(columns={
        "Item ID": "guid", "PLP Views": "plp_views",
        "PLP Clicks": "plp_clicks", "PLP ATC": "plp_atc",
    })
    df = df.merge(plp[["guid", "plp_views", "plp_clicks", "plp_atc"]], on="guid", how="left")

    # ---- M4: PDP metrics --------------------------------------------------
    # This extract carries the whole catalogue (~12.9k rows, 20 categories) and
    # has no ID column, so we match on name AND restrict to the flower category.
    # Eight names in the file are duplicated across categories; none are ours,
    # but the category filter is what guarantees a 1:1 join.
    pdp = pdp[pdp["Item category"] == "flower"].copy()
    pdp["nm"] = norm_name(pdp["Item name"])
    df["nm"] = norm_name(df["title"])
    # Duplicates exist inside the flower category (e.g. "intellect flower card
    # product" appears twice) but are irrelevant unless they collide with one of
    # our SKUs — which would silently fan out the join and double-count M4.
    collisions = pdp[pdp["nm"].duplicated(keep=False) & pdp["nm"].isin(set(df["nm"]))]["nm"].unique()
    if len(collisions):
        sys.exit(f"PDP names collide with our SKUs, join is unsafe: {list(collisions)[:5]}")
    pdp = pdp[pdp["nm"].isin(set(df["nm"]))]
    df = df.merge(
        pdp[["nm", "PDP Views", "PDP ATC", "PDP Purchase"]].rename(columns={
            "PDP Views": "pdp_views", "PDP ATC": "pdp_atc", "PDP Purchase": "pdp_purchase",
        }),
        on="nm", how="left",
    )

    # ---- M3, M5: sales ----------------------------------------------------
    sales["order_date"] = pd.to_datetime(sales["order_date"], errors="coerce")
    window = sales[(sales["order_date"] >= WINDOW_START) & (sales["order_date"] <= WINDOW_END)]

    # M3 denominator: every flower order in the window, not just our 240 SKUs.
    total_orders = int(window["order_id"].nunique())

    agg = window.groupby("nid").agg(
        orders=("order_id", "nunique"),
        units=("qty", "sum"),
        revenue=("order_total", "sum"),
    )
    df = df.merge(agg, left_on="guid", right_index=True, how="left")

    # Recency uses the FULL sales history, not the window — a SKU last sold in
    # April is stale regardless of which window we score on.
    last_order = sales.groupby("nid")["order_date"].max()
    df = df.merge(last_order.rename("last_order_date"), left_on="guid", right_index=True, how="left")
    df["days_since_order"] = (AS_OF - df["last_order_date"]).dt.days

    # ---- Control sequence -------------------------------------------------
    # No ID column in this extract, so matched on name. Verified 240/240 with no
    # collisions. Production must join on GUID: two SKUs here differ only by an
    # apostrophe ("Fathers Day Special Box" / "Father's Day Special Box").
    sequence.columns = [c.strip() for c in sequence.columns]
    sequence["nm"] = norm_name(sequence["Product Name"])
    df = df.merge(sequence[["nm", "Index"]].rename(columns={"Index": "current_index"}), on="nm", how="left")
    df["current_rank"] = df["current_index"].rank(method="first").astype("Int64")

    # ---- Integrity checks -------------------------------------------------
    problems = []
    for col, label in [
        ("plp_views", "PLP metrics"), ("pdp_views", "PDP metrics"), ("current_index", "sequence"),
    ]:
        missing = df[df[col].isna()]
        if len(missing):
            problems.append(f"{len(missing)} SKU(s) missing {label}: {missing['title'].head(3).tolist()}")
    if problems:
        print("WARNING — incomplete joins:", *problems, sep="\n  ")

    df["orders"] = df["orders"].fillna(0)
    df["units"] = df["units"].fillna(0)
    df["revenue"] = df["revenue"].fillna(0)

    # A SKU created after the window opened cannot have accumulated a fair
    # history. Flagged so the viewer can route it to the cold-start default
    # instead of scoring it at zero.
    df["is_new"] = df["created"] > WINDOW_START

    # ---- Peer median price (M6 benchmark) ---------------------------------
    # Median, not mean: one Rs 45,045 SKU drags the Bunches mean to Rs 2,395
    # against a median of Rs 995, which would mark 70% of the group
    # "below average" and flatten the modifier.
    live = df[df["on_page"]]
    peer_median = live.groupby("peer_group")["price"].median().to_dict()
    page_median = float(live["price"].median())

    # ---- Page-level pooled rates (smoothing priors) -----------------------
    priors = {
        "m1": float(live["plp_clicks"].sum() / live["plp_views"].sum()),
        "m2": float(live["plp_atc"].sum() / live["plp_clicks"].sum()),
        "m4": float(live["pdp_atc"].sum() / live["pdp_views"].sum()),
    }

    records = []
    for r in df.itertuples(index=False):
        records.append({
            "guid": int(r.guid),
            "title": r.title,
            "sku": r.sku,
            "url": r.url,
            "price": int(r.price),
            "mrp": int(r.mrp),
            "peerGroup": r.peer_group,
            "peerRaw": r.peer_raw,
            "peerMedian": float(peer_median.get(r.peer_group, page_median)),
            "onPage": bool(r.on_page),
            "isNew": bool(r.is_new),
            "bestSeller": int(r.best_seller),
            "created": r.created.strftime("%Y-%m-%d") if pd.notna(r.created) else None,
            "currentIndex": int(r.current_index) if pd.notna(r.current_index) else None,
            "plpViews": int(r.plp_views or 0),
            "plpClicks": int(r.plp_clicks or 0),
            "plpAtc": int(r.plp_atc or 0),
            "pdpViews": int(r.pdp_views or 0),
            "pdpAtc": int(r.pdp_atc or 0),
            "pdpPurchase": int(r.pdp_purchase or 0),
            "orders": int(r.orders),
            "units": int(r.units),
            "revenue": float(r.revenue),
            "lastOrder": r.last_order_date.strftime("%Y-%m-%d") if pd.notna(r.last_order_date) else None,
            "daysSinceOrder": int(r.days_since_order) if pd.notna(r.days_since_order) else None,
        })

    records.sort(key=lambda x: (x["currentIndex"] is None, x["currentIndex"]))

    payload = {
        "meta": {
            "page": "/all-flowers",
            "generated": AS_OF.strftime("%Y-%m-%d"),
            "windowStart": WINDOW_START.strftime("%Y-%m-%d"),
            "windowEnd": WINDOW_END.strftime("%Y-%m-%d"),
            "windowConfirmed": False,
            "skuCount": len(records),
            "liveSkuCount": int(df["on_page"].sum()),
            "totalFlowerOrders": total_orders,
            "pagePriors": priors,
            "pageMedianPrice": page_median,
            "peerMedians": {k: float(v) for k, v in peer_median.items()},
            "peerCounts": live["peer_group"].value_counts().to_dict(),
            "minPeerGroup": MIN_PEER_GROUP,
            "defaultWeights": {"A": 0.45, "B": 0.25, "C": 0.20, "D": 0.10, "E": 1.40, "F": 1.40},
        },
        "products": records,
    }
    return payload


if __name__ == "__main__":
    payload = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, separators=(",", ":"))
    OUT.write_text(blob)
    OUT_JS.write_text("window.WSM_DATA=" + blob + ";")
    m = payload["meta"]
    print(f"Wrote {OUT.relative_to(ROOT)}")
    print(f"  {m['skuCount']} SKUs ({m['liveSkuCount']} live) | window {m['windowStart']} .. {m['windowEnd']}")
    print(f"  M3 denominator: {m['totalFlowerOrders']:,} flower orders")
    print(f"  priors: M1 {m['pagePriors']['m1']:.4f}  M2 {m['pagePriors']['m2']:.4f}  M4 {m['pagePriors']['m4']:.4f}")

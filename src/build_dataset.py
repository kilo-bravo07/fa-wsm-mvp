"""
Listing Page Product Sequencing — dataset builder (v3).

Merges three product pools into one catalogue:

    flowers   240  price + launch date + orders from the sales extract
    combos     60  from the /combos/flowers-and-cakes GA4 extracts
    hampers    62  from the /hampers/same-day-delivery GA4 extracts

Output: docs/data.json and docs/data.js, one record per product with RAW metric
inputs only. Normalisation, smoothing and allocation happen client-side so the
weights and the template can be moved live.

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
OUT_JS = ROOT / "docs" / "data.js"

CFG = json.loads((ROOT / "config" / "config.json").read_text())
WINDOW_END = pd.Timestamp(CFG["window"]["flowers_orders_end"])
WINDOW_START = WINDOW_END - pd.Timedelta(days=CFG["window"]["days"] - 1)


def _read(name: str, **kw) -> pd.DataFrame:
    path = DATA / name
    if not path.exists():
        sys.exit(f"Missing source file: {path}\nSee data/README.md for the expected extracts.")
    if path.suffix == ".xlsx":
        return pd.read_excel(path, **kw)
    return pd.read_csv(path, **kw)


def norm_name(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower()


def build_flowers() -> pd.DataFrame:
    products = _read("products.csv")
    plp = _read("plp_metrics.csv")
    pdp = _read("pdp_metrics.csv")
    sales = _read("sales.csv")
    sequence = _read("current_sequence.csv")

    df = products.rename(columns={"GUID": "guid", "Title": "title", "Sku": "sku"})
    df["created"] = pd.to_datetime(
        df["Create Date and Time"].astype(str).str.split(", ").str[-1],
        format="%m/%d/%Y - %H:%M", errors="coerce",
    )
    # `Page display` is null for most rows; only an explicit "Donot display"
    # takes a product off the page, corroborated by its zero impressions in GA4.
    df["on_page"] = df["Page display"].fillna("Display").str.strip() != "Donot display"
    df = df[["guid", "title", "sku", "url", "Sell Price", "created", "on_page"]].rename(
        columns={"Sell Price": "price"}
    )
    df["productType"] = "Flower"

    plp = plp.rename(columns={
        "Item ID": "guid", "PLP Views": "plpViews",
        "PLP Clicks": "plpClicks", "PLP ATC": "plpAtc",
    })
    df = df.merge(plp[["guid", "plpViews", "plpClicks", "plpAtc"]], on="guid", how="left")

    # The PDP extract carries the whole catalogue and has no ID column, so match
    # on name within the flower category. Abort on a collision rather than let
    # the join silently fan out.
    pdp = pdp[pdp["Item category"] == "flower"].copy()
    pdp["nm"] = norm_name(pdp["Item name"])
    df["nm"] = norm_name(df["title"])
    clash = pdp[pdp["nm"].duplicated(keep=False) & pdp["nm"].isin(set(df["nm"]))]["nm"].unique()
    if len(clash):
        sys.exit(f"PDP names collide with flower SKUs, join is unsafe: {list(clash)[:5]}")
    df = df.merge(
        pdp[["nm", "PDP Views", "PDP ATC"]].rename(
            columns={"PDP Views": "pdpViews", "PDP ATC": "pdpAtc"}),
        on="nm", how="left",
    )

    sales["order_date"] = pd.to_datetime(sales["order_date"], errors="coerce")
    window = sales[(sales["order_date"] >= WINDOW_START) & (sales["order_date"] <= WINDOW_END)]
    agg = window.groupby("nid").agg(orders=("order_id", "nunique"), revenue=("order_total", "sum"))
    df = df.merge(agg, left_on="guid", right_index=True, how="left")

    sequence.columns = [c.strip() for c in sequence.columns]
    sequence["nm"] = norm_name(sequence["Product Name"])
    df = df.merge(
        sequence[["nm", "Index"]].rename(columns={"Index": "currentIndex"}), on="nm", how="left"
    )
    return df.drop(columns=["nm"])


def build_category(plp_file: str, pdp_file: str, product_type: str) -> pd.DataFrame:
    """Combos and hampers: GA4 only — no price, no launch date, no order extract.

    Both extracts repeat every row several times; the duplicates are identical,
    so dropping them is safe. Rows with no Item ID cannot be joined to anything
    and are dropped with a notice.
    """
    plp = _read(plp_file).drop_duplicates()
    pdp = _read(pdp_file).drop_duplicates()

    missing_id = plp["Item ID"].isna().sum()
    if missing_id:
        print(f"  {product_type}: dropped {missing_id} row(s) with no Item ID "
              f"({', '.join(plp[plp['Item ID'].isna()]['Item name'].unique()[:3])})")
    plp = plp[plp["Item ID"].notna()].copy()
    plp["guid"] = plp["Item ID"].astype(int)

    plp = plp.rename(columns={
        "PLP Views": "plpViews", "PLP Clicks": "plpClicks", "PLP ATC": "plpAtc",
        "Item name": "title",
    })
    plp = plp.drop_duplicates("guid")

    pdp = pdp.drop_duplicates("Item name").copy()
    pdp["nm"] = norm_name(pdp["Item name"])
    plp["nm"] = norm_name(plp["title"])
    pdp = pdp.rename(columns={
        "PDP Views": "pdpViews", "PDP ATC": "pdpAtc", "PDP Purchase": "orders",
    })

    df = plp[["guid", "title", "nm", "plpViews", "plpClicks", "plpAtc"]].merge(
        pdp[["nm", "pdpViews", "pdpAtc", "orders"]], on="nm", how="left"
    )
    df["productType"] = product_type
    df["price"] = np.nan          # not in any extract yet
    df["created"] = pd.NaT
    df["sku"] = None
    df["url"] = None
    df["on_page"] = True
    df["currentIndex"] = np.nan
    df["revenue"] = np.nan        # no revenue source for these pools
    return df.drop(columns=["nm"])


def build() -> dict:
    flowers = build_flowers()
    combos = build_category(
        "plp_combos.csv", "pdp_combos.csv", "Combo")
    hampers = build_category(
        "plp_hampers.csv", "pdp_hampers.csv", "Hamper")

    df = pd.concat([flowers, combos, hampers], ignore_index=True)

    dupes = df[df["guid"].duplicated(keep=False)]["guid"].unique()
    if len(dupes):
        sys.exit(f"duplicate GUID across pools, would place a product twice: {list(dupes)[:5]}")

    for col in ("plpViews", "plpClicks", "plpAtc", "pdpViews", "pdpAtc", "orders"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Stock is not in any extract. Defaulting to in-stock keeps the pipeline
    # running; the viewer flags that the three stock rules are untested.
    df["inStock"] = True

    live = df[df["on_page"]]
    priors = {
        "ctr": float(live["plpClicks"].sum() / max(live["plpViews"].sum(), 1)),
        "atc": float(live["pdpAtc"].sum() / max(live["plpClicks"].sum(), 1)),
        "conversion": float(live["orders"].sum() / max(live["plpClicks"].sum(), 1)),
    }

    records = []
    for r in df.itertuples(index=False):
        records.append({
            "guid": int(r.guid),
            "title": r.title,
            "sku": r.sku if isinstance(r.sku, str) else None,
            "url": r.url if isinstance(r.url, str) else None,
            "productType": r.productType,
            "price": None if pd.isna(r.price) else int(r.price),
            "created": r.created.strftime("%Y-%m-%d") if pd.notna(r.created) else None,
            "onPage": bool(r.on_page),
            "inStock": bool(r.inStock),
            "currentIndex": None if pd.isna(r.currentIndex) else int(r.currentIndex),
            "plpViews": int(r.plpViews), "plpClicks": int(r.plpClicks), "plpAtc": int(r.plpAtc),
            "pdpViews": int(r.pdpViews), "pdpAtc": int(r.pdpAtc),
            "orders": int(r.orders),
            "revenue": None if pd.isna(r.revenue) else float(r.revenue),
        })

    counts = df["productType"].value_counts().to_dict()
    payload = {
        "meta": {
            "page": "/all-flowers",
            "generated": WINDOW_END.strftime("%Y-%m-%d"),
            "windowStart": WINDOW_START.strftime("%Y-%m-%d"),
            "windowEnd": WINDOW_END.strftime("%Y-%m-%d"),
            "windowConfirmed": False,
            "stockDataAvailable": False,
            "skuCount": len(records),
            "poolCounts": counts,
            "pagePriors": priors,
            "sources": {
                "Flower": "/all-flowers",
                "Combo": "/combos/flowers-and-cakes",
                "Hamper": "/hampers/same-day-delivery",
            },
            "config": CFG,
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
    print(f"Wrote {OUT.relative_to(ROOT)} and {OUT_JS.relative_to(ROOT)}")
    print(f"  {m['skuCount']} products: " + ", ".join(f"{v} {k.lower()}s" for k, v in m["poolCounts"].items()))
    print(f"  flower order window {m['windowStart']} .. {m['windowEnd']}")
    print(f"  priors: CTR {m['pagePriors']['ctr']:.4f}  ATC {m['pagePriors']['atc']:.4f}  "
          f"conversion {m['pagePriors']['conversion']:.4f}")

"""
Listing Page Product Sequencing — reference implementation (v3).

Built to the tiering and page order in "FA Listing Sequence Algorithm":

    Step 1  score()     every product gets a 0-1 score from four signals
    Step 2  allocate()  products are dealt into a repeating 30-slot template
    Step 3              the same template continues to the end of the catalogue

The viewer (docs/index.html) reimplements both in JavaScript so weights and the
template can be changed live; tests/test_sequence.py asserts the two agree.

SCORING
    CTR          clicks / impressions          35%
    ATC rate     add-to-carts / clicks         25%
    Conversion   orders / clicks               20%
    Orders       order count                   20%

The two "/ clicks" denominators are what the PRD specifies. They are a
configurable switch because on this data they misbehave: combos and hampers are
measured on their own category pages, their orders arrive largely through routes
that never touched the listing, and 18 of 122 have no clicks at all. Smoothing
and the impression floor contain the damage; `denominators` lets you see what
PDP views would do instead.

Each signal is normalised 0-1 across the page before weighting, because they are
rates on very different scales and summing them raw would let one dominate
through its units alone.

SLOTTING
    Seven buckets, 30 slots, repeating:

        L  M  C  L  U  P  M  L  H  C
        U  L  P  M  V  L  C  U  P  M
        L  H  C  U  P  M  V  L  C  P

    L/M/U/H/V are price bands, C is combos, P is hampers. Each slot takes the
    highest-scoring unclaimed product from its bucket. A product is claimed by
    the first bucket in `precedence` it qualifies for, which is what guarantees
    one-SKU-one-placement.

    Pools are finite. The PRD says an empty pool borrows from a later batch,
    which cannot terminate once a pool is globally exhausted and leaves a hole
    that cascades. Instead a price slot falls through to the nearest band by
    price, then to the best remaining product of any bucket.

    Out-of-stock products are barred from the first N positions and sink to the
    end of the page.

BOOSTS
    Up to four per 30-slot set, inserted at configured positions AFTER the base
    set is built. They sit outside the 30, so a set with x insertions displays
    30+x products and the base order never changes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

SIGNALS = ("ctr", "atc", "conversion", "orders")


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    cfg = json.loads(Path(path).read_text())

    total = sum(cfg["weights"][s] for s in SIGNALS)
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"weights must sum to 1.0, got {total}")

    slots = sum(b["slots"] for b in cfg["buckets"].values())
    if slots != len(cfg["template"]):
        raise ValueError(f"bucket slots total {slots} but template has {len(cfg['template'])} positions")

    counted = pd.Series(cfg["template"]).value_counts().to_dict()
    for key, b in cfg["buckets"].items():
        if counted.get(key, 0) != b["slots"]:
            raise ValueError(
                f"bucket {key} declares {b['slots']} slots but appears {counted.get(key, 0)} times in the template"
            )

    missing = set(cfg["buckets"]) - set(cfg["precedence"])
    if missing:
        raise ValueError(f"precedence is missing bucket(s): {sorted(missing)}")

    return cfg


# --------------------------------------------------------------------------
# Step 1 — scoring
# --------------------------------------------------------------------------

def smooth(successes: pd.Series, trials: pd.Series, prior: float, k: float) -> pd.Series:
    """Shrink a ratio toward `prior`, weighted by k pseudo-observations.

    The phantom product is k trials AND k*prior successes — padding only the
    denominator would drag every product toward zero rather than the average.
    """
    trials = pd.to_numeric(trials, errors="coerce").fillna(0).astype(float)
    successes = pd.to_numeric(successes, errors="coerce").fillna(0).astype(float)
    if k <= 0:
        return (successes / trials.replace(0, np.nan)).fillna(prior)
    return (successes + k * prior) / (trials + k)


def minmax(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").fillna(0).astype(float)
    lo, hi = s.min(), s.max()
    if hi == lo:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def percentile(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0).rank(pct=True)


def normalise(s: pd.Series, method: str) -> pd.Series:
    return percentile(s) if method == "percentile" else minmax(s)


def _denominator(df: pd.DataFrame, which: str) -> pd.Series:
    return df["plpClicks"] if which == "clicks" else df["pdpViews"]


def score(products: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Attach raw signals, normalised signals and the composite `score`."""
    df = products.copy()
    k = cfg["smoothing_k"]
    w = cfg["weights"]
    dn = cfg["denominators"]
    method = cfg.get("normalisation", "minmax")

    atc_d = _denominator(df, dn["atc"])
    cvr_d = _denominator(df, dn["conversion"])

    pooled_ctr = df["plpClicks"].sum() / max(df["plpViews"].sum(), 1)
    pooled_atc = df["pdpAtc"].sum() / max(atc_d.sum(), 1)
    pooled_cvr = df["orders"].sum() / max(cvr_d.sum(), 1)

    df["ctr_raw"] = smooth(df["plpClicks"], df["plpViews"], pooled_ctr, k)
    df["atc_raw"] = smooth(df["pdpAtc"], atc_d, pooled_atc, k / 10)
    df["conversion_raw"] = smooth(df["orders"], cvr_d, pooled_cvr, k / 30)
    df["orders_raw"] = pd.to_numeric(df["orders"], errors="coerce").fillna(0).astype(float)

    # Normalised values live in `<signal>_n`, never back on the source column —
    # writing them onto `orders` would destroy the raw count the table and the
    # CSV read.
    for s in SIGNALS:
        df[f"{s}_n"] = normalise(df[f"{s}_raw"], method)

    df["score"] = sum(df[f"{s}_n"] * w[s] for s in SIGNALS)

    # Below the floor a product has not been seen enough for its own rates to
    # mean anything. It takes the median score of its own bucket instead, so a
    # thin combo is judged against combos rather than against the whole page.
    floor = cfg["impression_floor"]
    df["below_floor"] = pd.to_numeric(df["plpViews"], errors="coerce").fillna(0) < floor
    if df["below_floor"].any():
        df = assign_buckets(df, cfg)
        med = df[~df["below_floor"]].groupby("bucket")["score"].median()
        page_med = df.loc[~df["below_floor"], "score"].median()
        if pd.isna(page_med):
            page_med = df["score"].median()
        df.loc[df["below_floor"], "score"] = (
            df.loc[df["below_floor"], "bucket"].map(med).fillna(page_med)
        )

    return df


# --------------------------------------------------------------------------
# Step 2/3 — bucket allocation
# --------------------------------------------------------------------------

def assign_buckets(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Claim each product for the first bucket in `precedence` it qualifies for."""
    df = df.copy()
    bucket = pd.Series(pd.NA, index=df.index, dtype="object")
    price = pd.to_numeric(df.get("price"), errors="coerce")

    for key in cfg["precedence"]:
        spec = cfg["buckets"][key]
        if spec["kind"] == "category":
            ok = df["productType"].astype(str).str.lower() == str(spec["value"]).lower()
        else:
            lo = spec["min"]
            hi = spec["max"]
            ok = price.notna() & (price >= lo)
            if hi is not None:
                ok &= price <= hi
        bucket = bucket.where(bucket.notna(), pd.Series(np.where(ok, key, pd.NA), index=df.index))

    df["bucket"] = bucket
    return df


def _price_order(cfg: dict) -> list[str]:
    bands = [(k, v) for k, v in cfg["buckets"].items() if v["kind"] == "price"]
    bands.sort(key=lambda kv: kv[1]["min"])
    return [k for k, _ in bands]


def fallback_chain(key: str, cfg: dict) -> list[str]:
    """Nearest price band outward, then anything. C and P go straight to anything."""
    spec = cfg["buckets"][key]
    if spec["kind"] != "price":
        return [key, "*"]
    order = _price_order(cfg)
    i = order.index(key)
    rest = sorted(
        [b for b in order if b != key],
        key=lambda b: (abs(order.index(b) - i), order.index(b)),
    )
    return [key] + rest + ["*"]


def allocate(scored: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Deal every product into the repeating template. One row per product."""
    df = assign_buckets(scored, cfg).copy()
    rules = cfg["rules"]
    top_n = rules["exclude_out_of_stock_from_top_n"]

    if "inStock" not in df:
        df["inStock"] = True
    df["inStock"] = df["inStock"].fillna(True).astype(bool)

    order = df.sort_values(
        ["score", "orders_raw", "plpViews", "guid"], ascending=[False, False, False, True]
    )
    pools: dict[str, list] = {
        key: [g for g, b in zip(order["guid"], order["bucket"]) if b == key]
        for key in cfg["buckets"]
    }
    # "*" is every product, used only once a slot's own chain is exhausted.
    pools["*"] = list(order["guid"])

    by = {r.guid: r for r in df.itertuples(index=False)}
    placed: set = set()
    in_stock = set(df.loc[df["inStock"], "guid"])

    def take(slot: str, position: int):
        for depth, name in enumerate(fallback_chain(slot, cfg)):
            pool = pools.get(name, [])
            while pool:
                guid = pool[0]
                if guid in placed:
                    pool.pop(0)
                    continue
                if position <= top_n and guid not in in_stock:
                    nxt = next((g for g in pool if g not in placed and g in in_stock), None)
                    if nxt is None:
                        break
                    guid = nxt
                pool.remove(guid)
                placed.add(guid)
                return guid, name, depth > 0
        return None

    template = cfg["template"]
    rows = []
    position = 0
    set_no = 0

    while len(placed) < len(df):
        set_no += 1
        progressed = False
        for slot in template:
            if len(placed) >= len(df):
                break
            position += 1
            got = take(slot, position)
            if got is None:
                position -= 1
                continue
            guid, from_pool, was_fallback = got
            progressed = True
            rows.append({
                "position": position, "set": set_no, "slot": slot,
                "filled_by": from_pool, "fallback": was_fallback, "guid": guid,
            })
        if not progressed:
            break

    out = pd.DataFrame(rows).merge(df, on="guid", how="left")

    if rules["out_of_stock_to_end"]:
        out = out.sort_values(["inStock", "position"], ascending=[False, True])
        out["position"] = range(1, len(out) + 1)
        out["set"] = (out["position"] - 1) // len(template) + 1

    return out.reset_index(drop=True)


def apply_boosts(base: pd.DataFrame, catalogue: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Insert boosted SKUs at configured positions, outside the base sequence.

    Each insertion adds a product rather than replacing one, so a set carrying x
    insertions shows 30+x. The relative order of the base products is untouched.
    """
    entries = [e for e in cfg["boost"]["entries"] if not str(e.get("guid", "")).startswith("_")]
    base = base.copy()
    base["boost"] = False
    if not entries:
        return base

    size = len(cfg["template"])
    per_set = cfg["boost"]["max_per_set"]

    # The template deals the whole catalogue, so a boosted SKU is already
    # somewhere on the page. It is LIFTED from that natural position and
    # re-inserted at the configured one — which is what makes a set carry 30+x
    # while every SKU still appears exactly once (requirement 2). The products
    # it jumps keep their relative order.
    rows = {r["guid"]: r for r in base.to_dict("records")}

    entries = sorted(entries, key=lambda e: (e.get("priority", 999), entries.index(e)))

    accepted, used_per_set, lifted = [], {}, set()
    for e in entries:
        guid = e["guid"]
        row = rows.get(guid)
        if row is None or guid in lifted:
            continue                                   # not on the page, or already boosted
        if not bool(row.get("inStock", True)):
            continue                                   # out of stock never gets a boost
        pos = int(e["position"])
        s = (pos - 1) // size
        if used_per_set.get(s, 0) >= per_set:
            continue                                   # that set already has its four
        used_per_set[s] = used_per_set.get(s, 0) + 1
        lifted.add(guid)
        accepted.append((pos, guid))

    if not accepted:
        return base

    seq = [r for r in base.sort_values("position").to_dict("records") if r["guid"] not in lifted]

    for pos, guid in sorted(accepted, key=lambda t: t[0], reverse=True):
        row = dict(rows[guid])
        row.update({"slot": "F", "filled_by": "boost", "fallback": False, "boost": True})
        seq.insert(min(max(pos - 1, 0), len(seq)), row)

    out = pd.DataFrame(seq)
    out["position"] = range(1, len(out) + 1)
    out["set"] = (out["position"] - 1) // size + 1
    return out.reset_index(drop=True)


def pool_report(scored: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Supply against the seats the template asks for, per bucket."""
    df = assign_buckets(scored, cfg)
    n_sets = math.ceil(len(df) / len(cfg["template"]))
    rows = []
    for key, spec in cfg["buckets"].items():
        supply = int((df["bucket"] == key).sum())
        demand = spec["slots"] * n_sets
        rows.append({
            "bucket": key, "label": spec["label"], "slots_per_set": spec["slots"],
            "supply": supply, "demand": demand,
            "shortfall": max(demand - supply, 0),
            "sets_covered": supply // spec["slots"] if spec["slots"] else 0,
        })
    return pd.DataFrame(rows)

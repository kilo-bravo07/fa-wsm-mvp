"""
Listing Page Product Sequencing — reference implementation.

Two stages, matching the PRD:

    Step 1  score()      every product gets a 0-1 score from four signals
    Step 2  allocate()   products are dealt into a repeating 10-slot template

The viewer (docs/index.html) reimplements both in JavaScript so the weights and
the template can be changed live; tests/test_parity.py asserts the two agree.

SCORING (Step 1)
    CTR             PLP clicks / PLP impressions        35%
    ATC PDP rate    PDP add-to-cart / PDP views         25%
    Conversion      orders / PLP clicks                 20%
    Orders          order count                         20%

All four are normalised 0-1 across the page before weighting, because they are
rates on very different scales and summing them raw would let one dominate
through its units alone. The three ratios are first shrunk toward the page's
pooled rate in proportion to how little traffic supports them — otherwise a SKU
with 5 clicks and 2 add-to-carts posts a 40% rate and outranks real performers.

SLOTTING (Step 2/3)
    The page is dealt in batches of 10. Each position in a batch has a slot type
    and each type draws the highest-scoring unplaced product from its pool:

        M  medium price band      H  high price band
        N  new arrival            F  featured, from the boost list

    Pools are finite. When one empties the slot falls through `fallback_chain`,
    which is what lets the strict price band govern the top of the page (where
    the mix matters) while still guaranteeing every product is placed exactly
    once. LOW is the implicit band below M — products cheaper than M.min, which
    belong to no band the PRD names.

    Out-of-stock products are barred from the first N positions and sink to the
    end of the page.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

SIGNALS = ("ctr", "atc_pdp", "conversion", "orders")


def load_config(path: Path | str = CONFIG_PATH) -> dict:
    cfg = json.loads(Path(path).read_text())
    total = sum(cfg["weights"][s] for s in SIGNALS)
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"weights must sum to 1.0, got {total}")
    for name, chain in cfg["fallback_chain"].items():
        if name.startswith("_"):
            continue
        if not chain or chain[0] != name:
            raise ValueError(f"fallback_chain[{name}] must start with {name!r}")
    return cfg


# --------------------------------------------------------------------------
# Step 1 — scoring
# --------------------------------------------------------------------------

def smooth(successes: pd.Series, trials: pd.Series, prior: float, k: float) -> pd.Series:
    trials = trials.fillna(0).astype(float)
    successes = successes.fillna(0).astype(float)
    if k <= 0:
        return (successes / trials.replace(0, np.nan)).fillna(prior)
    return (successes + k * prior) / (trials + k)


def minmax(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    lo, hi = s.min(), s.max()
    if hi == lo:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def score(products: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Attach raw signals, normalised signals and the composite `score`."""
    df = products.copy()
    k = cfg["smoothing_k"]
    w = cfg["weights"]

    pooled_ctr = df["plpClicks"].sum() / max(df["plpViews"].sum(), 1)
    pooled_atc = df["pdpAtc"].sum() / max(df["pdpViews"].sum(), 1)
    pooled_cvr = df["orders"].sum() / max(df["plpClicks"].sum(), 1)

    df["ctr_raw"] = smooth(df["plpClicks"], df["plpViews"], pooled_ctr, k)
    df["atc_pdp_raw"] = smooth(df["pdpAtc"], df["pdpViews"], pooled_atc, k / 10)
    df["conversion_raw"] = smooth(df["orders"], df["plpClicks"], pooled_cvr, k / 30)
    df["orders_raw"] = df["orders"].fillna(0).astype(float)

    # Normalised values live in `<signal>_n`, never in `<signal>` — writing them
    # back onto the source column would destroy the raw `orders` count that the
    # table, the CSV and the KPIs all read.
    for s in SIGNALS:
        df[f"{s}_n"] = minmax(df[f"{s}_raw"])

    df["score"] = sum(df[f"{s}_n"] * w[s] for s in SIGNALS)
    return df


# --------------------------------------------------------------------------
# Step 2/3 — slot allocation
# --------------------------------------------------------------------------

def assign_bands(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Tag each product with its price band: LOW, M or H."""
    bands = cfg["price_bands"]
    m_min, m_max = bands["M"]["min"], bands["M"]["max"]
    h_min = bands["H"]["min"]

    df = df.copy()
    band = pd.Series("LOW", index=df.index)
    band[df["price"].between(m_min, m_max)] = "M"
    band[df["price"] >= h_min] = "H"
    df["band"] = band
    return df


def new_arrival_flags(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Recompute the N pool from `created` so new_arrival_days stays a live knob.

    The build bakes an `isNewArrival` column at one fixed horizon; reading that
    column here would silently ignore any change to new_arrival_days.
    """
    cutoff = pd.Timestamp(cfg["window"]["end"]) - pd.Timedelta(days=cfg["new_arrival_days"])
    created = pd.to_datetime(df["created"], errors="coerce")
    manual = set(cfg["manual_new"]["guids"])
    return (created > cutoff).fillna(False) | df["guid"].isin(manual)


def build_template(n_batches: int, cfg: dict) -> list[list[str]]:
    t = cfg["templates"]
    out = []
    for i in range(n_batches):
        if i == 0:
            out.append(list(t["batch_1"]))
        elif i == 1:
            out.append(list(t["batch_2"]))
        else:
            out.append(list(t["batch_default"]))
    return out


@dataclass
class Placement:
    position: int
    batch: int
    slot: str          # the slot the template asked for
    filled_by: str     # the pool it was actually drawn from
    guid: int
    fallback: bool


def allocate(scored: pd.DataFrame, cfg: dict, seed: int = 7) -> pd.DataFrame:
    """Deal every product into the slot template. Returns one row per product."""
    df = assign_bands(scored, cfg).copy()
    rules = cfg["rules"]
    top_n = rules["exclude_out_of_stock_from_top_n"]

    if "inStock" not in df:
        df["inStock"] = True
    df["inStock"] = df["inStock"].fillna(True).astype(bool)

    boost = set(cfg["boost_list"]["guids"])
    df["isBoost"] = df["guid"].isin(boost)
    df["isNew"] = new_arrival_flags(df, cfg)

    # A boosted SKU is held out of the ordinary price pools, otherwise an M slot
    # takes it before its F seat comes round — the boost buys nothing and the F
    # seat falls back. It rejoins the general pools only once every F seat the
    # template asks for has been dealt.
    reserve = cfg["rules"].get("reserve_boost_for_f", True)

    order = df.sort_values("score", ascending=False)
    held = boost if reserve else set()
    pools: dict[str, list[int]] = {
        "M": [g for g, b in zip(order["guid"], order["band"]) if b == "M" and g not in held],
        "H": [g for g, b in zip(order["guid"], order["band"]) if b == "H" and g not in held],
        "LOW": [g for g, b in zip(order["guid"], order["band"]) if b == "LOW" and g not in held],
        "N": [g for g, n in zip(order["guid"], order["isNew"]) if n and g not in held],
    }
    # The F pool is a random draw from the boost list, not a score sort — but it
    # must be stable for a given seed so the slot does not change on refresh
    # within a session (requirement 6).
    rng = np.random.default_rng(seed)
    f_pool = [g for g in order["guid"] if g in boost]
    rng.shuffle(f_pool)
    pools["F"] = f_pool

    in_stock = set(df.loc[df["inStock"], "guid"])
    placed: set[int] = set()
    chains = {k: v for k, v in cfg["fallback_chain"].items() if not k.startswith("_")}

    def take(slot: str, position: int) -> tuple[int, str, bool] | None:
        """Pull the best unplaced product for this slot, following the chain."""
        for depth, pool_name in enumerate(chains.get(slot, [slot])):
            pool = pools.get(pool_name, [])
            while pool:
                guid = pool[0]
                if guid in placed:
                    pool.pop(0)
                    continue
                # Requirement 3: out-of-stock never appears in the top N.
                if position <= top_n and guid not in in_stock:
                    # Leave it in the pool for a later position; scan past it.
                    nxt = next((g for g in pool if g not in placed and g in in_stock), None)
                    if nxt is None:
                        break
                    guid = nxt
                pool.remove(guid)
                placed.add(guid)
                return guid, pool_name, depth > 0
        return None

    n_batches = math.ceil(len(df) / 10)
    rows: list[Placement] = []
    position = 0

    for b, batch in enumerate(build_template(n_batches, cfg)):
        for slot in batch:
            if len(placed) >= len(df):
                break
            position += 1
            got = take(slot, position)
            if got is None:
                position -= 1
                continue
            guid, pool_name, was_fallback = got
            rows.append(Placement(position, b + 1, slot, pool_name, guid, was_fallback))

    # Any boosted SKU that never got an F seat rejoins the tail by score, along
    # with anything the template could not consume (pools emptied unevenly).
    # Out-of-stock products sink below everything afterwards.
    leftover = [g for g in order["guid"] if g not in placed]
    for guid in leftover:
        position += 1
        rows.append(Placement(position, (position - 1) // 10 + 1, "-", "overflow", guid, True))

    out = pd.DataFrame([r.__dict__ for r in rows])
    out = out.merge(df, on="guid", how="left")

    if rules["out_of_stock_to_end"]:
        out = out.sort_values(["inStock", "position"], ascending=[False, True])
        out["position"] = range(1, len(out) + 1)
        out["batch"] = (out["position"] - 1) // 10 + 1

    return out.reset_index(drop=True)


def pool_report(scored: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Supply vs demand per slot type — the check that tells you the template fits."""
    df = assign_bands(scored, cfg)
    is_new = new_arrival_flags(df, cfg)

    n_batches = math.ceil(len(df) / 10)
    demand: dict[str, int] = {}
    for batch in build_template(n_batches, cfg):
        for slot in batch:
            demand[slot] = demand.get(slot, 0) + 1

    supply = {
        "M": int((df["band"] == "M").sum()),
        "H": int((df["band"] == "H").sum()),
        "N": int(is_new.sum()),
        "F": len(cfg["boost_list"]["guids"]),
        "LOW": int((df["band"] == "LOW").sum()),
    }
    rows = [
        {"slot": s, "supply": supply.get(s, 0), "demand": demand.get(s, 0),
         "shortfall": max(demand.get(s, 0) - supply.get(s, 0), 0)}
        for s in ("M", "H", "N", "F", "LOW")
    ]
    return pd.DataFrame(rows)

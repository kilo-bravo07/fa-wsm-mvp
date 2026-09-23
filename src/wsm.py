"""
Weighted Sequence Model — reference scorer.

This is the authoritative definition of the scoring maths. The viewer
(docs/index.html) reimplements it in JavaScript so the weights can be moved
live; `tests/test_parity.py` asserts the two agree.

Formula (per the spec):
    S1 = M1*A + M2*B + M3*C + M4*D
    S2 = S1 * E   (if the recency condition holds)
    RS = S2 * F   (if the price condition holds)
Higher RS ranks earlier on the page.

Three things the spec left open, and what this implementation does about them:

normalisation
    M1-M4 are rates on different scales (CTR ~1.6%, ATC ~18%, order share
    ~0.09%). Summing them raw would let M2 dominate purely by unit. Each metric
    is mapped to 0-1 across the page first. Min-max is the default; percentile
    rank is the robust alternative when a single outlier compresses everyone
    else.

smoothing
    A SKU with 5 clicks and 2 add-to-carts shows a 40% ATC rate. Unsmoothed it
    outranks genuinely strong products. Each ratio is shrunk toward the page
    pooled rate in proportion to how little traffic supports it — with k
    pseudo-observations, a product needs roughly k impressions before its own
    rate dominates the prior.

cold start
    A SKU created after the window opened has no fair history. Scoring it at
    zero buries it permanently, which is a self-fulfilling prophecy. It is
    assigned the median score of live products instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_WEIGHTS = {"A": 0.45, "B": 0.25, "C": 0.20, "D": 0.10, "E": 1.40, "F": 1.40}


@dataclass
class Config:
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    normalisation: str = "minmax"        # "minmax" | "percentile" | "zscore"
    smoothing_k: float = 3000.0          # pseudo-observations; 0 disables
    recency_days: int = 90               # spec: "last 3 months"
    price_below_median_wins: bool = True  # M6 direction
    cold_start_median: bool = True


def smooth(successes: pd.Series, trials: pd.Series, prior: float, k: float) -> pd.Series:
    """Shrink a ratio toward `prior`, weighted by k pseudo-observations."""
    trials = trials.fillna(0).astype(float)
    successes = successes.fillna(0).astype(float)
    if k <= 0:
        return (successes / trials.replace(0, np.nan)).fillna(prior)
    return (successes + k * prior) / (trials + k)


def normalise(s: pd.Series, method: str) -> pd.Series:
    s = s.astype(float)
    if method == "percentile":
        return s.rank(pct=True)
    if method == "zscore":
        sd = s.std(ddof=0)
        if sd == 0:
            return pd.Series(0.5, index=s.index)
        z = (s - s.mean()) / sd
        return ((z + 3) / 6).clip(0, 1)
    lo, hi = s.min(), s.max()
    if hi == lo:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def score(products: pd.DataFrame, priors: dict, total_orders: int, cfg: Config) -> pd.DataFrame:
    """products: one row per SKU with the raw columns from build_dataset.py."""
    df = products.copy()
    w = cfg.weights

    df["m1_raw"] = smooth(df["plpClicks"], df["plpViews"], priors["m1"], cfg.smoothing_k)
    df["m2_raw"] = smooth(df["plpAtc"], df["plpClicks"], priors["m2"], cfg.smoothing_k / 30)
    df["m3_raw"] = df["orders"].fillna(0) / max(total_orders, 1)
    df["m4_raw"] = smooth(df["pdpAtc"], df["pdpViews"], priors["m4"], cfg.smoothing_k / 10)

    for m in ("m1", "m2", "m3", "m4"):
        df[m] = normalise(df[f"{m}_raw"], cfg.normalisation)

    df["s1"] = df["m1"] * w["A"] + df["m2"] * w["B"] + df["m3"] * w["C"] + df["m4"] * w["D"]

    # M5 — recency. Missing last-order date is treated as stale, not recent.
    df["m5_hit"] = df["daysSinceOrder"].fillna(10**6) <= cfg.recency_days
    df["s2"] = df["s1"] * np.where(df["m5_hit"], w["E"], 1.0)

    # M6 — price against the peer-group median.
    cheaper = df["price"] < df["peerMedian"]
    df["m6_hit"] = cheaper if cfg.price_below_median_wins else ~cheaper
    df["rs"] = df["s2"] * np.where(df["m6_hit"], w["F"], 1.0)

    if cfg.cold_start_median:
        established = df.loc[~df["isNew"].astype(bool), "rs"]
        if len(established):
            df.loc[df["isNew"].astype(bool) & (df["orders"].fillna(0) == 0), "rs"] = established.median()

    df["wsm_rank"] = df["rs"].rank(ascending=False, method="first").astype(int)
    return df.sort_values("rs", ascending=False)

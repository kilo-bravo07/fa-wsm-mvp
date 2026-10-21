"""
Guards the v3 sequencing engine.

1. PRD requirements — each hard rule asserted directly, so a config edit that
   breaks one fails here rather than on the live page.
2. Scorer/allocator parity — docs/index.html reimplements src/sequence.py in
   JavaScript so the controls can move live. If the two drift, the page shows
   one sequence and the spec documents another.

    python -m pytest tests/ -q          (node required for the parity tests)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sequence import (  # noqa: E402
    allocate, apply_boosts, assign_buckets, fallback_chain, load_config, pool_report, score,
)

DATA_PATH = ROOT / "docs" / "data.json"
pytestmark = pytest.mark.skipif(
    not DATA_PATH.exists(), reason="run `python src/build_dataset.py` first"
)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def payload():
    return json.loads(DATA_PATH.read_text())


@pytest.fixture(scope="module")
def live(payload):
    return pd.DataFrame([p for p in payload["products"] if p["onPage"]])


@pytest.fixture(scope="module")
def seq(live, cfg):
    return allocate(score(live, cfg), cfg)


# --- config integrity ------------------------------------------------------

def test_config_is_internally_consistent():
    """load_config rejects a template whose counts contradict the bucket slots."""
    load_config()  # raises on mismatch

    bad = json.loads((ROOT / "config" / "config.json").read_text())
    bad["template"][0] = "C"          # L loses a slot, C gains one
    tmp = ROOT / "config" / "_test_bad.json"
    tmp.write_text(json.dumps(bad))
    try:
        with pytest.raises(ValueError, match="slots"):
            load_config(tmp)
    finally:
        tmp.unlink()


def test_template_is_thirty_slots(cfg):
    assert len(cfg["template"]) == 30
    counts = pd.Series(cfg["template"]).value_counts().to_dict()
    assert counts == {"L": 7, "M": 5, "U": 4, "C": 5, "P": 5, "H": 2, "V": 2}


# --- PRD requirements ------------------------------------------------------

def test_req2_each_product_appears_once(seq, live):
    assert len(seq) == len(live), "every product must be placed"
    assert seq["guid"].is_unique, "a product appears only once on the page"


def test_positions_are_contiguous(seq):
    assert seq["position"].tolist() == list(range(1, len(seq) + 1))


def test_first_set_matches_the_template(seq, cfg):
    first = seq[seq["position"] <= 30].sort_values("position")["slot"].tolist()
    assert first == cfg["template"]


def test_first_set_is_filled_from_its_own_buckets(seq):
    """With a healthy catalogue no slot in set 1 should need the fallback."""
    first = seq[seq["position"] <= 30]
    assert not first["fallback"].any(), (
        f"set 1 used fallback for: {first[first['fallback']][['slot', 'filled_by']].to_dict('records')}"
    )


def test_buckets_are_mutually_exclusive(live, cfg):
    df = assign_buckets(score(live, cfg), cfg)
    combos = df[df["productType"] == "Combo"]
    hampers = df[df["productType"] == "Hamper"]
    assert (combos["bucket"] == "C").all(), "combo eligibility must outrank price bands"
    assert (hampers["bucket"] == "P").all(), "hamper eligibility must outrank price bands"
    assert df["bucket"].notna().all(), "every product must land in exactly one bucket"


def test_req3_no_out_of_stock_in_top_10(live, cfg):
    df = score(live, cfg).copy()
    victims = df.nlargest(5, "score")["guid"].tolist()
    df["inStock"] = ~df["guid"].isin(victims)
    out = allocate(df, cfg)
    top = out[out["position"] <= cfg["rules"]["exclude_out_of_stock_from_top_n"]]
    assert top["inStock"].all(), "out-of-stock product reached the top 10"


def test_out_of_stock_sinks_to_the_end(live, cfg):
    df = score(live, cfg).copy()
    victims = set(df.nlargest(6, "score")["guid"])
    df["inStock"] = ~df["guid"].isin(victims)
    out = allocate(df, cfg)
    assert set(out.tail(len(victims))["guid"]) == victims


def test_fallback_chain_prefers_the_nearest_price_band(cfg):
    assert fallback_chain("L", cfg)[:2] == ["L", "M"], "Under ₹1,000 should fall to ₹1,000–1,499"
    assert fallback_chain("V", cfg)[:2] == ["V", "H"], "Above ₹2,500 should fall to ₹2,000–2,499"
    assert fallback_chain("U", cfg)[0] == "U"
    assert fallback_chain("C", cfg) == ["C", "*"], "combo falls straight through"
    for key in cfg["buckets"]:
        assert fallback_chain(key, cfg)[-1] == "*", "every chain must end in a catch-all"


def test_raw_orders_survive_scoring(live, cfg):
    """Normalised signals must not overwrite the raw columns they derive from."""
    scored = score(live, cfg)
    assert (scored["orders"].values == live["orders"].values).all()
    assert scored["orders_n"].between(0, 1).all()


# --- boost insertions ------------------------------------------------------

def test_boost_inserts_at_the_requested_position(live, cfg, seq):
    cfg2 = json.loads(json.dumps(cfg))
    target = int(seq.iloc[200]["guid"])          # something well down the page
    cfg2["boost"]["entries"] = [{"guid": target, "position": 5}]
    out = apply_boosts(seq, score(live, cfg2), cfg2)
    assert int(out.iloc[4]["guid"]) == target
    assert out.iloc[4]["slot"] == "F"
    assert bool(out.iloc[4]["boost"])


def test_boost_keeps_one_sku_one_placement(live, cfg, seq):
    cfg2 = json.loads(json.dumps(cfg))
    targets = [int(seq.iloc[i]["guid"]) for i in (150, 200, 250)]
    cfg2["boost"]["entries"] = [
        {"guid": g, "position": p} for g, p in zip(targets, (3, 7, 11))
    ]
    out = apply_boosts(seq, score(live, cfg2), cfg2)
    assert out["guid"].is_unique, "a boosted SKU must not appear twice"
    assert len(out) == len(seq), "boosting moves a product, it does not clone one"


def test_boost_preserves_base_order(live, cfg, seq):
    cfg2 = json.loads(json.dumps(cfg))
    target = int(seq.iloc[200]["guid"])
    cfg2["boost"]["entries"] = [{"guid": target, "position": 5}]
    out = apply_boosts(seq, score(live, cfg2), cfg2)
    before = [g for g in seq["guid"] if g != target]
    after = [g for g in out["guid"] if g != target]
    assert before == after, "inserting must not re-rank the rest of the page"


def test_boost_respects_the_per_set_cap(live, cfg, seq):
    cfg2 = json.loads(json.dumps(cfg))
    cap = cfg2["boost"]["max_per_set"]
    targets = [int(seq.iloc[150 + i]["guid"]) for i in range(cap + 3)]
    cfg2["boost"]["entries"] = [
        {"guid": g, "position": i + 1} for i, g in enumerate(targets)
    ]
    out = apply_boosts(seq, score(live, cfg2), cfg2)
    first_set = out[out["position"] <= len(cfg["template"]) + cap]
    assert int(first_set["boost"].sum()) <= cap, f"more than {cap} insertions landed in set 1"


def test_boost_skips_out_of_stock(live, cfg, seq):
    cfg2 = json.loads(json.dumps(cfg))
    target = int(seq.iloc[200]["guid"])
    base = seq.copy()
    base.loc[base["guid"] == target, "inStock"] = False
    cfg2["boost"]["entries"] = [{"guid": target, "position": 5}]
    out = apply_boosts(base, score(live, cfg2), cfg2)
    assert not bool(out.iloc[4].get("boost", False)), "an out-of-stock SKU must not be boosted"


def test_req7_empty_boost_list_is_a_no_op(live, cfg, seq):
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["boost"]["entries"] = []
    out = apply_boosts(seq, score(live, cfg2), cfg2)
    assert out["guid"].tolist() == seq["guid"].tolist()


# --- pool reporting --------------------------------------------------------

def test_pool_report_covers_every_bucket(live, cfg):
    rep = pool_report(score(live, cfg), cfg)
    assert set(rep["bucket"]) == set(cfg["buckets"])
    assert (rep["supply"] >= 0).all()


# --- Python / JavaScript parity -------------------------------------------

def _run_js(overrides: dict, tmp_path: Path) -> list[int]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    html = (ROOT / "docs" / "index.html").read_text()
    start = html.index("/* ---------- Step 1: score")
    end = html.index("/* ---------- header ---------- */")
    engine = html[start:end]

    harness = tmp_path / "h.mjs"
    harness.write_text(textwrap.dedent(f"""
        import {{ readFileSync }} from 'fs';
        const DATA = JSON.parse(readFileSync({str(DATA_PATH)!r},'utf8'));
        const META = DATA.meta;
        const OV = {json.dumps(overrides)};
        const CFG0 = {{...META.config, ...OV}};
        const ALL = DATA.products.filter(p => p.onPage);
        const SIGNALS = ['ctr','atc','conversion','orders'];
        const BK = CFG0.buckets, BKEYS = Object.keys(BK);
        let W = CFG0.weights, K = CFG0.smoothing_k;
        let NORM = CFG0.normalisation || 'minmax';
        let FLOOR = CFG0.impression_floor;
        let DN = CFG0.denominators;
        let template = CFG0.template;
        let boosts = (CFG0.boost.entries || []).map(e => ({{guid: e.guid, position: e.position}}));
        {engine}
        const sc = scoreAll();
        console.log(JSON.stringify(applyBoosts(allocate(sc), sc).map(r => r.guid)));
    """))
    res = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize("overrides", [
    {},
    {"smoothing_k": 0},
    {"normalisation": "percentile"},
    {"denominators": {"atc": "pdp_views", "conversion": "pdp_views"}},
    {"weights": {"ctr": 0.25, "atc": 0.25, "conversion": 0.25, "orders": 0.25}},
    {"impression_floor": 0},
    {"template": ["C", "P"] * 15, "buckets": None},
], ids=["defaults", "no-smoothing", "percentile", "pdp-denominators",
        "equal-weights", "no-floor", "combo-hamper-only"])
def test_js_matches_python(payload, live, cfg, overrides, tmp_path):
    merged = json.loads(json.dumps(cfg))
    ov = json.loads(json.dumps(overrides))
    ov.pop("buckets", None)          # the marker that a template override skips validation
    merged.update(ov)

    py = allocate(score(live, merged), merged)
    py = apply_boosts(py, score(live, merged), merged)["guid"].tolist()
    js = _run_js(ov, tmp_path)

    assert len(js) == len(py)
    if js != py:
        titles = {p["guid"]: p["title"] for p in payload["products"]}
        i = next(i for i, (a, b) in enumerate(zip(js, py)) if a != b)
        pytest.fail(
            f"sequences diverge at position {i + 1}: "
            f"JS has {titles[js[i]]!r}, Python has {titles[py[i]]!r}"
        )


def test_js_matches_python_with_boosts(payload, live, cfg, seq, tmp_path):
    targets = [int(seq.iloc[i]["guid"]) for i in (120, 180, 240)]
    ov = {"boost": {**cfg["boost"], "entries": [
        {"guid": g, "position": p} for g, p in zip(targets, (4, 9, 15))
    ]}}
    merged = json.loads(json.dumps(cfg))
    merged.update(json.loads(json.dumps(ov)))

    py = allocate(score(live, merged), merged)
    py = apply_boosts(py, score(live, merged), merged)["guid"].tolist()
    js = _run_js(json.loads(json.dumps(ov)), tmp_path)
    assert js == py, "boosted sequences diverge between the two implementations"

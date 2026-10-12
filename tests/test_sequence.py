"""
Guards the sequencing engine.

1. PRD requirements — each hard requirement from section 6 is asserted directly,
   so a config edit that breaks one fails here rather than on the live page.
2. Scorer/allocator parity — docs/index.html reimplements src/sequence.py in
   JavaScript so the controls can move live. If the two drift, the page shows one
   sequence and the spec documents another.

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

from sequence import allocate, load_config, pool_report, score  # noqa: E402

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


# --- PRD section 6 requirements -------------------------------------------

def test_req2_each_product_appears_once(seq, live):
    assert len(seq) == len(live), "every product must be placed"
    assert seq["guid"].is_unique, "a product appears only once on the page"


def test_positions_are_contiguous(seq):
    assert seq["position"].tolist() == list(range(1, len(seq) + 1))


def test_req3_no_out_of_stock_in_top_10(live, cfg):
    """Force a few top scorers out of stock and check they leave the top 10."""
    df = score(live, cfg)
    victims = df.nlargest(4, "score")["guid"].tolist()
    df = df.copy()
    df["inStock"] = ~df["guid"].isin(victims)
    out = allocate(df, cfg)
    top = out[out["position"] <= cfg["rules"]["exclude_out_of_stock_from_top_n"]]
    assert top["inStock"].all(), "out-of-stock product reached the top 10"


def test_out_of_stock_sinks_to_the_end(live, cfg):
    df = score(live, cfg).copy()
    victims = set(df.nlargest(6, "score")["guid"])
    df["inStock"] = ~df["guid"].isin(victims)
    out = allocate(df, cfg)
    tail = out.tail(len(victims))
    assert set(tail["guid"]) == victims, "out-of-stock products must occupy the page tail"


def test_req7_empty_boost_list_falls_back(live, cfg):
    cfg = json.loads(json.dumps(cfg))
    cfg["boost_list"]["guids"] = []
    out = allocate(score(live, cfg), cfg)
    f = out[out["slot"] == "F"]
    assert len(f) > 0
    assert (f["filled_by"] != "F").all(), "F must fall back when the boost list is empty"


def test_boost_list_fills_f_slots(live, cfg):
    cfg = json.loads(json.dumps(cfg))
    picks = live["guid"].head(5).tolist()
    cfg["boost_list"]["guids"] = picks
    out = allocate(score(live, cfg), cfg)
    f_from_boost = out[(out["slot"] == "F") & (out["filled_by"] == "F")]
    assert len(f_from_boost) == len(picks), "every boosted SKU should take an F seat"
    assert set(f_from_boost["guid"]) == set(picks)


def test_manual_new_seeds_the_n_pool(live, cfg):
    base = pool_report(score(live, cfg), cfg)
    base_n = int(base.loc[base["slot"] == "N", "supply"].iloc[0])

    cfg2 = json.loads(json.dumps(cfg))
    extra = [g for g in live["guid"].tolist() if g not in cfg["manual_new"]["guids"]][:7]
    cfg2["manual_new"]["guids"] = extra
    after = pool_report(score(live, cfg2), cfg2)
    after_n = int(after.loc[after["slot"] == "N", "supply"].iloc[0])
    assert after_n > base_n, "manual new-arrival list must enlarge the N pool"


def test_batch_1_follows_the_template(seq, cfg):
    first = seq[seq["batch"] == 1].sort_values("position")["slot"].tolist()
    assert first == cfg["templates"]["batch_1"]


def test_raw_orders_survive_scoring(live, cfg):
    """Normalised signals must not overwrite the raw columns they derive from."""
    scored = score(live, cfg)
    assert (scored["orders"] == live["orders"].values).all()
    assert scored["orders_n"].between(0, 1).all()


# --- Python / JavaScript parity -------------------------------------------

def _run_js(cfg_overrides: dict, tmp_path: Path) -> list[int]:
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
        const OV = {json.dumps(cfg_overrides)};
        const CFG0 = {{...META.config, ...OV}};
        const ALL = DATA.products.filter(p => p.onPage);
        const SIGNALS = ['ctr','atc_pdp','conversion','orders'];
        let W = CFG0.weights, K = CFG0.smoothing_k;
        let bands = CFG0.price_bands, templates = CFG0.templates;
        let newDays = CFG0.new_arrival_days;
        let boost = CFG0.boost_list.guids, manualNew = CFG0.manual_new.guids;
        const CHAIN = CFG0.fallback_chain, RULES = CFG0.rules;
        {engine}
        console.log(JSON.stringify(allocate(scoreAll()).map(r => r.guid)));
    """))
    res = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize("overrides", [
    {},
    {"smoothing_k": 0},
    {"weights": {"ctr": 0.25, "atc_pdp": 0.25, "conversion": 0.25, "orders": 0.25}},
    {"price_bands": {"M": {"min": 500, "max": 1500, "label": "Medium"},
                     "H": {"min": 1501, "max": None, "label": "High"}}},
    {"new_arrival_days": 120},
], ids=["defaults", "no-smoothing", "equal-weights", "wider-bands", "new-120d"])
def test_js_matches_python(payload, live, cfg, overrides, tmp_path):
    merged = json.loads(json.dumps(cfg))
    merged.update(json.loads(json.dumps(overrides)))
    py = allocate(score(live, merged), merged)["guid"].tolist()
    js = _run_js(overrides, tmp_path)

    assert len(js) == len(py)
    if js != py:
        titles = {p["guid"]: p["title"] for p in payload["products"]}
        i = next(i for i, (a, b) in enumerate(zip(js, py)) if a != b)
        pytest.fail(
            f"sequences diverge at position {i + 1}: "
            f"JS has {titles[js[i]]!r}, Python has {titles[py[i]]!r}"
        )

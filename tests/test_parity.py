"""
Guards the two things most likely to break quietly.

1. Join integrity — every live SKU must carry PLP metrics, PDP metrics and a
   current position. A silent left-join miss would score a product at zero and
   bury it, and nothing on the page would say so.
2. Scorer parity — docs/index.html reimplements src/wsm.py in JavaScript so the
   weights can move live. If the two drift, the page shows one ranking and the
   spec documents another. This runs both over the same data and asserts the
   orderings match exactly.

    python -m pytest tests/ -q          (node required for the parity test)
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

from wsm import Config, score  # noqa: E402

DATA_PATH = ROOT / "docs" / "data.json"
pytestmark = pytest.mark.skipif(
    not DATA_PATH.exists(), reason="run `python src/build_dataset.py` first"
)


@pytest.fixture(scope="module")
def payload():
    return json.loads(DATA_PATH.read_text())


@pytest.fixture(scope="module")
def live(payload):
    return pd.DataFrame([p for p in payload["products"] if p["onPage"]])


def test_no_duplicate_guids(payload):
    guids = [p["guid"] for p in payload["products"]]
    assert len(guids) == len(set(guids))


def test_every_live_sku_has_metrics(live):
    for col in ("plpViews", "pdpViews", "currentIndex"):
        missing = live[live[col].isna()]
        assert missing.empty, f"{col} missing for {missing['title'].tolist()[:5]}"


def test_no_impossible_rates(live):
    assert (live["plpClicks"] <= live["plpViews"]).all(), "clicks exceed impressions"
    assert (live["pdpAtc"] <= live["pdpViews"]).all(), "PDP ATC exceeds PDP views"


def test_peer_groups_meet_floor(payload):
    floor = payload["meta"]["minPeerGroup"]
    for group, n in payload["meta"]["peerCounts"].items():
        assert n >= floor or group == "Other", f"{group} has only {n} SKUs"


@pytest.mark.parametrize(
    "cfg",
    [
        Config(),
        Config(normalisation="percentile"),
        Config(smoothing_k=0),
        Config(recency_days=7),
        Config(price_below_median_wins=False),
        Config(weights={"A": 0.1, "B": 0.1, "C": 0.7, "D": 0.1, "E": 1.0, "F": 1.8}),
    ],
    ids=["spec", "percentile", "no-smoothing", "recency-7d", "m6-inverted", "order-heavy"],
)
def test_js_matches_python(payload, live, cfg, tmp_path):
    """Run the viewer's scorer in node and compare rankings with the Python one."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    html = (ROOT / "docs" / "index.html").read_text()
    start = html.index("function smooth(")
    end = html.index("/* ---------- header + KPIs ---------- */")
    scorer = html[start:end]

    harness = tmp_path / "harness.mjs"
    harness.write_text(textwrap.dedent(f"""
        import {{ readFileSync }} from 'fs';
        const DATA = JSON.parse(readFileSync({str(DATA_PATH)!r}, 'utf8'));
        const M = DATA.meta;
        const ALL = DATA.products.filter(p => p.onPage);
        let W = {json.dumps(cfg.weights)};
        let cfg = {{
            norm: {cfg.normalisation!r},
            k: {cfg.smoothing_k},
            recency: {cfg.recency_days},
            m6: {"below" if cfg.price_below_median_wins else "above"!r},
            cold: {str(cfg.cold_start_median).lower()},
        }};
        {scorer}
        const out = compute();
        console.log(JSON.stringify(out.map(o => o.guid)));
    """))

    res = subprocess.run([node, str(harness)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    js_order = json.loads(res.stdout)

    py_order = score(
        live, payload["meta"]["pagePriors"], payload["meta"]["totalFlowerOrders"], cfg
    )["guid"].tolist()

    assert len(js_order) == len(py_order)
    if js_order != py_order:
        first = next(i for i, (a, b) in enumerate(zip(js_order, py_order)) if a != b)
        titles = {p["guid"]: p["title"] for p in payload["products"]}
        pytest.fail(
            f"rankings diverge at position {first + 1}: "
            f"JS has {titles[js_order[first]]!r}, Python has {titles[py_order[first]]!r}"
        )

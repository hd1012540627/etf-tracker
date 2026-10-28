"""Offline tests on real data captured from the issuers on 2026-10-28.

Run:  python -m pytest -q
"""
import copy
import html
import json
from pathlib import Path

from tracker import analytics, symbols
from tracker.adapters import allianz, capitalfund, ezmoney
from tracker.prices import PriceStore, load_fixture

FX = Path(__file__).parent / "fixtures"
CFG = {c["code"]: c for c in json.load(open(Path(__file__).parents[1] / "config" / "etfs.json"))["etfs"]}


def load(name):
    return json.load(open(FX / name, encoding="utf-8"))


def test_symbols():
    assert symbols.parse("2330").key == "TW:2330"
    s = symbols.parse("6857 JP")
    assert (s.key, s.currency, s.yahoo) == ("JP:6857", "JPY", "6857.T")
    assert symbols.parse("002463 C2").yahoo == "002463.SZ"
    assert symbols.parse("600183 C1").yahoo == "600183.SS"
    assert symbols.parse("000660 KS").yahoo == "000660.KS"
    assert symbols.parse("AVGO US").key == "US:AVGO"


def test_allianz_parse():
    s = allianz.parse(load("allianz_E0004_20261029.json"), CFG["00412A"])
    assert len(s["holdings"]) == 58
    assert s["pcf_date"] == "2026-10-29" and s["position_date"] == "2026-10-28"
    assert abs(sum(h["weight"] for h in s["holdings"]) - 85.65) < 0.01
    assert abs(s["assets"]["rp_twd"] - 755_100_000) < 1
    assert s["units"] == 852_988_000


def test_ezmoney_parse_and_html_extract():
    raw = ezmoney.fixture_to_raw(open(FX / "ezmoney_49YTW_20261028.txt", encoding="utf-8").readlines())
    s = ezmoney.parse(raw, CFG["00981A"])
    assert len(s["holdings"]) == 50
    assert s["position_date"] == "2026-10-28"
    fut = s["assets"]["futures"][0]
    assert fut["side"] == "long" and fut["contracts"] == 679
    # the live site embeds the JSON HTML-escaped in a data-content attribute
    page = f'<div id="DataAsset" data-content="{html.escape(json.dumps(raw, ensure_ascii=False))}"></div>'
    assert len(ezmoney.extract_asset_json(page)) == len(raw)


def test_capitalfund_parse_fx():
    s = capitalfund.parse(load("capitalfund_502_20261029.json"), CFG["00997A"])
    assert len(s["holdings"]) == 48
    assert abs(s["assets"]["fx"]["USD"] - 32.475) < 1e-9
    assert abs(s["assets"]["fx"]["JPY"] - 32.475 / 157.81) < 1e-9
    # USD cash converted to TWD
    assert s["assets"]["cash_twd"] > 19_361_960 * 32
    assert s["position_date"] == "2026-10-28"


def _store():
    st = PriceStore.__new__(PriceStore)
    st.hist, st.live, st.symmap, st.errors = {}, {}, {}, []
    load_fixture(st, FX / "yahoo_prices_20261028.txt")
    return st


def test_enrich_and_rules():
    st = _store()
    s = ezmoney.parse(ezmoney.fixture_to_raw(open(FX / "ezmoney_49YTW_20261028.txt", encoding="utf-8").readlines()),
                      CFG["00981A"])
    e = analytics.enrich(s, st, {})
    m = e["metrics"]
    assert abs(m["stock_pct"] - 96.1) < 0.05
    assert abs(m["effective_equity_pct"] - (m["stock_pct"] + 2.29)) < 0.05
    assert not m["missing_price"]
    tsmc = next(h for h in e["holdings"] if h["key"] == "TW:2330")
    assert tsmc["close"] == 2425  # matches 統一's own Amount / Share


def test_flow_adjusted_diff_and_cost():
    st = _store()
    base = ezmoney.parse(ezmoney.fixture_to_raw(open(FX / "ezmoney_49YTW_20261028.txt", encoding="utf-8").readlines()),
                         CFG["00981A"])
    day2 = copy.deepcopy(base)
    day2["units"] *= 1.10            # 10% creation
    for h in day2["holdings"]:
        h["shares"] *= 1.10          # proportional purchase only
    tsmc = next(h for h in day2["holdings"] if h["key"] == "TW:2330")
    tsmc["shares"] += 100_000        # plus a real manager buy
    e1, e2 = analytics.enrich(base, st, {}), analytics.enrich(day2, st, {})
    d = analytics.diff(e1, e2)
    rows = {r["key"]: r for r in d["rows"]}
    assert rows["TW:2330"]["status"] == "加碼"
    assert abs(rows["TW:2330"]["active_shares"] - 100_000) < 1
    assert rows["TW:2454"]["status"] == "隨申贖等比例變動"
    costs = analytics.estimate_costs([e1, e2], [analytics.diff(None, e1), d])
    assert costs["TW:2330"]["avg_cost_local"] > 0


def test_split_detection():
    st = _store()
    a = allianz.parse(load("allianz_E0004_20261029.json"), CFG["00412A"])
    b = copy.deepcopy(a)
    b["holdings"][0]["shares"] *= 2
    e1, e2 = analytics.enrich(a, st, {}), analytics.enrich(b, st, {})
    e2["holdings"][0]["close"] = e1["holdings"][0]["close"] / 2
    d = analytics.diff(e1, e2)
    assert d["rows"][0]["status"] == "公司行動" or any(r["status"] == "公司行動" for r in d["rows"])

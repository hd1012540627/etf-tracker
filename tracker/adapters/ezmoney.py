"""統一投信 (www.ezmoney.com.tw).

The fund page embeds the full asset breakdown as HTML-escaped JSON in
<div id="DataAsset" data-content="...">. Groups (AssetCode):
  NAV 淨資產, OUT_UNIT 流通在外單位數, P_UNIT 每單位淨值,
  GD 期貨(名目本金) [details: TX 台指期貨, Position B/S, Share=口數, MTH=月份],
  ST 股票 [details: DetailCode, DetailName, Share, Amount (TWD), NavRate (% of NAV)],
  CASH 現金, GDM 期貨保證金, RP 附買回債券, APAR 應收付證券款 ...
TranDate is the position date (published ~16:40 the same day, 2026-10-28 observed).
"""
from __future__ import annotations

import html as htmlmod
import json
import re

from .. import http, symbols
from ..util import num, parse_date

BASE = "https://www.ezmoney.com.tw"


def page_url(params: dict) -> str:
    return f"{BASE}/ETF/Fund/Info?fundCode={params['fund_code']}"


def extract_asset_json(page_html: str) -> list:
    m = re.search(r'<[^>]*\bid="DataAsset"[^>]*>', page_html)
    if not m:
        raise ValueError("ezmoney: DataAsset element not found")
    tag = m.group(0)
    m2 = re.search(r'data-content="([^"]*)"', tag)
    if not m2:
        raise ValueError("ezmoney: data-content missing")
    txt = htmlmod.unescape(m2.group(1))
    # the site double-escapes (&amp;quot;) in some versions
    if txt.lstrip().startswith("[") is False and "&quot;" in txt:
        txt = htmlmod.unescape(txt)
    return json.loads(txt)


def fetch_raw(params: dict) -> list:
    if http.browser_enabled():
        js = """() => {
          const p=new DOMParser();
          const raw=document.getElementById('DataAsset').getAttribute('data-content');
          return JSON.parse(p.parseFromString(raw,'text/html').documentElement.textContent);
        }"""
        return http.browser_eval(page_url(params), js, wait_ms=1500)
    s = http.session()
    r = http.request(s, "GET", page_url(params))
    return extract_asset_json(r.text)


GROUP_TO_FIELD = {"CASH": "cash_twd", "GDM": "futures_margin_twd", "RP": "rp_twd",
                  "APAR": "receivable_payable_twd", "BD": "bond_twd", "BOND": "bond_twd"}


def parse(raw: list, cfg: dict) -> dict:
    from . import new_snapshot
    snap = new_snapshot(cfg, page_url(cfg["params"]))
    a = snap["assets"]
    pos_date = None
    for g in raw:
        code = g.get("AssetCode")
        val = num(g.get("Value"))
        details = g.get("Details") or []
        for d in details:
            dd = parse_date(d.get("TranDate"))
            if dd:
                pos_date = dd
        if code == "NAV":
            snap["nav_twd"] = val
        elif code == "OUT_UNIT":
            snap["units"] = val
        elif code == "P_UNIT":
            snap["nav_per_unit"] = val
        elif code == "ST":
            a["stock_twd"] = val
            for d in details:
                sym = symbols.parse(str(d.get("DetailCode")))
                snap["holdings"].append({
                    "key": sym.key, "code_raw": d.get("DetailCode"), "name": (d.get("DetailName") or "").strip(),
                    "market": sym.market, "currency": sym.currency, "shares": num(d.get("Share")),
                    "weight": num(d.get("NavRate")), "amount_twd": num(d.get("Amount")),
                })
        elif code == "GD":
            for d in details:
                a["futures"].append({
                    "code": d.get("DetailCode"), "name": d.get("DetailName"),
                    "side": "long" if (d.get("Position") or "").strip().upper() == "B" else "short",
                    "contracts": num(d.get("Share")), "notional_twd": num(d.get("Amount")),
                    "month": d.get("MTH"), "weight": num(d.get("NavRate")),
                })
        elif code in GROUP_TO_FIELD:
            a[GROUP_TO_FIELD[code]] += val or 0.0
            if code == "RP":
                a["rp_items"].append({"name": "附買回債券", "amount_twd": val})
        else:
            a["other_twd"] += val or 0.0
            a["other_items"].append({"name": g.get("AssetName") or code, "amount_twd": val})
    if pos_date is None:
        for g in raw:
            dd = parse_date(g.get("EditDate"))
            if dd:
                pos_date = dd
                break
    snap["position_date"] = pos_date.isoformat() if pos_date else None
    snap["nav_date"] = snap["position_date"]
    if snap["nav_twd"] and a.get("stock_twd"):
        a["stock_pct_reported"] = round(a["stock_twd"] / snap["nav_twd"] * 100, 4)
    a["cash_is_residual"] = False
    return snap


def fixture_to_raw(lines: list[str]) -> list:
    """Convert the pipe-delimited test fixture back into the site's JSON shape."""
    groups: list[dict] = []
    for ln in lines:
        p = ln.rstrip("\n").split("|")
        if p[0] == "G":
            groups.append({"AssetCode": p[1], "AssetName": p[2], "Value": float(p[3]), "EditDate": p[4], "Details": []})
        elif p[0] == "D":
            groups[-1]["Details"].append({
                "AssetCode": p[1], "DetailCode": p[2], "DetailName": p[3], "Share": float(p[4]),
                "Amount": float(p[5]), "NavRate": float(p[6]), "Position": p[7], "MTH": p[8],
                "MoneyType": p[9], "TranDate": p[10] + "T00:00:00"})
    return groups

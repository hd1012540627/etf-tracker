"""群益投信 (www.capitalfund.com.tw).

API (found 2026-10-28 from the site's Angular front-end):
  POST /CFWeb/api/etf/buyback  {"fundId": "502", "date": null}
Response data: pcf {date1=PCF date, date2=NAV date, nav, totUnit, pUnit, exchangeDesc ...},
  stocks [{stocNo, stocName, share, weight}], futures [], bonds [], rps [],
  assets [{asDesc, asMoney: "USD 19,361,960.38"}].
The site is behind Imperva; if plain requests are blocked the browser fallback is used.
"""
from __future__ import annotations

import re

from .. import http, symbols
from ..util import num, parse_date, prev_weekday

BASE = "https://www.capitalfund.com.tw"


def page_url(params: dict) -> str:
    return f"{BASE}/etf/product/detail/{params['fund_id']}/portfolio"


def fetch_raw(params: dict) -> dict:
    fid = params["fund_id"]
    if http.browser_enabled():
        js = """async () => {
          const r=await fetch('/CFWeb/api/etf/buyback',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({fundId:'%s',date:null})});
          return await r.json();
        }""" % fid
        return http.browser_eval(page_url(params), js, wait_ms=5000)
    s = http.session()
    s.headers.update({"Origin": BASE, "Referer": page_url(params)})
    r = http.request(s, "POST", f"{BASE}/CFWeb/api/etf/buyback", json={"fundId": fid, "date": None})
    j = r.json()
    if j.get("code") != 200:
        raise http.Blocked(f"capitalfund: code {j.get('code')}")
    return j


def parse_fx(desc: str | None) -> dict[str, float]:
    """' 匯率：1 EUR = 1.1161 USD ,1 USD = 157.8100 JPY ,1 USD = 32.4750 TWD (2026/10/27)'
    -> {'USD': 32.475, 'JPY': 32.475/157.81, 'EUR': 1.1161*32.475, 'KRW': ...} (TWD per unit)."""
    if not desc:
        return {}
    pairs = re.findall(r"1\s*([A-Z]{3})\s*=\s*([\d.]+)\s*([A-Z]{3})", desc)
    usd_twd = next((float(v) for a, v, b in pairs if a == "USD" and b == "TWD"), None)
    out: dict[str, float] = {"TWD": 1.0}
    if usd_twd is None:
        return out
    out["USD"] = usd_twd
    for a, v, b in pairs:
        v = float(v)
        if a == "USD" and b != "TWD" and v:
            out[b] = usd_twd / v
        elif b == "USD" and a != "USD":
            out[a] = v * usd_twd
    return out


def parse(raw: dict, cfg: dict) -> dict:
    from . import new_snapshot
    snap = new_snapshot(cfg, page_url(cfg["params"]))
    d = raw.get("data") or {}
    pcf = d.get("pcf") or {}
    snap["nav_twd"] = num(pcf.get("nav"))
    snap["units"] = num(pcf.get("totUnit"))
    snap["nav_per_unit"] = num(pcf.get("pUnit"))
    pcf_date = parse_date(pcf.get("date1"))
    nav_date = parse_date(pcf.get("date2"))
    snap["pcf_date"] = pcf_date.isoformat() if pcf_date else None
    snap["nav_date"] = nav_date.isoformat() if nav_date else None
    snap["position_date"] = prev_weekday(pcf_date).isoformat() if pcf_date else None
    fx = parse_fx(pcf.get("exchangeDesc"))
    a = snap["assets"]
    a["fx"] = fx
    nav = snap["nav_twd"] or 0.0
    tot_w = 0.0
    for s in d.get("stocks") or []:
        sym = symbols.parse(str(s.get("stocNo")))
        w = num(s.get("weight"))
        tot_w += w or 0.0
        snap["holdings"].append({
            "key": sym.key, "code_raw": s.get("stocNo"), "name": (s.get("stocName") or "").strip(),
            "name_en": s.get("stocEname"), "market": sym.market, "currency": sym.currency,
            "shares": num(s.get("share")), "weight": w,
            "amount_twd": (w / 100 * nav) if (w is not None and nav) else None,
        })
    a["stock_pct_reported"] = round(tot_w, 4)
    a["stock_twd"] = tot_w / 100 * nav if nav else None

    def to_twd(money: str | None) -> float | None:
        if not money:
            return None
        m = re.match(r"\s*([A-Z]{3})\s*\$?\s*([-\d,\.]+)", str(money))
        if not m:
            return num(money)
        ccy, amt = m.group(1), num(m.group(2))
        rate = fx.get(ccy)
        if amt is None:
            return None
        if rate is None:
            snap["notes"].append(f"缺 {ccy} 匯率，{money} 未換算")
            return None
        return amt * rate

    for it in d.get("assets") or []:
        desc = (it.get("asDesc") or "").strip()
        v = to_twd(it.get("asMoney")) or 0.0
        if "保證金" in desc:
            a["futures_margin_twd"] += v
        elif desc.startswith("現金") or "存款" in desc:
            a["cash_twd"] += v
        elif "贖回" in desc:
            a["redemption_payable_twd"] += v
        elif "應收付" in desc or "應收" in desc or "應付" in desc:
            a["receivable_payable_twd"] += v
        else:
            a["other_twd"] += v
            a["other_items"].append({"name": desc, "amount_twd": v})
    for f in d.get("futures") or []:
        a["futures"].append({
            "code": f.get("futNo") or f.get("code"), "name": f.get("futName") or f.get("name"),
            "side": "short" if str(f.get("buySell") or f.get("side") or "").upper() in ("S", "SELL", "賣") else "long",
            "contracts": num(f.get("qty") or f.get("share") or f.get("lots")),
            "notional_twd": to_twd(f.get("amount")) if isinstance(f.get("amount"), str) else num(f.get("amount")),
            "month": f.get("month") or f.get("mth"), "raw": f,
        })
    for rp in d.get("rps") or []:
        v = to_twd(rp.get("rpMoney") or rp.get("amount")) if isinstance(rp.get("rpMoney") or rp.get("amount"), str) \
            else num(rp.get("rpMoney") or rp.get("amount"))
        a["rp_items"].append({"name": rp.get("rpName") or rp.get("name"), "amount_twd": v})
        a["rp_twd"] += v or 0.0
    for b in d.get("bonds") or []:
        v = num(b.get("amount") or b.get("bondMoney"))
        a["bond_twd"] += v or 0.0
    a["cash_is_residual"] = False
    return snap

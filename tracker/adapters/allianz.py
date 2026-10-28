"""安聯投信 (etf.allianzgi.com.tw).

API (found 2026-10-28 from the site's own front-end):
  GET  /webapi/api/AntiForgery/GetAntiForgeryToken   -> sets cookie X-XSRF-TOKEN
  POST /webapi/api/Fund/GetFundAssets  {"FundID": "E0004"} with header X-XSRF-TOKEN
Response: Entries.Data.FundAsset {Aum, Units, Nav, NavDate, PCFDate} and Table[] where
  Table[0]   summary rows e.g. ["股票","TWD$6,372,377,867","TWD","6,372,377,867"]
  "股票 (85.65%)"  rows [seq, code, name, shares, weight%]
  "附買回債券"      rows [seq, name, "TWD $ 55,000,000.00"]
The holdings are the 申購買回清單 (PCF) for PCFDate, i.e. positions after the previous
trading day's close.
"""
from __future__ import annotations

import re

from .. import http, symbols
from ..util import num, parse_date, prev_weekday

BASE = "https://etf.allianzgi.com.tw"


def page_url(params: dict) -> str:
    return f"{BASE}/etf-info/{params['fund_id']}?tab=4"


def fetch_raw(params: dict) -> dict:
    fid = params["fund_id"]
    if http.browser_enabled():
        js = """async () => {
          await fetch('/webapi/api/AntiForgery/GetAntiForgeryToken',{credentials:'include'});
          const c=document.cookie.split(';').map(s=>s.trim()).find(s=>s.startsWith('X-XSRF-TOKEN='))||'';
          const tok=decodeURIComponent(c.split('=').slice(1).join('='));
          const r=await fetch('/webapi/api/Fund/GetFundAssets',{method:'POST',credentials:'include',
            headers:{'Content-Type':'application/json','X-XSRF-TOKEN':tok},body:JSON.stringify({FundID:'%s'})});
          return await r.json();
        }""" % fid
        return http.browser_eval(page_url(params), js)
    s = http.session()
    s.headers.update({"Origin": BASE, "Referer": page_url(params)})
    http.request(s, "GET", f"{BASE}/webapi/api/AntiForgery/GetAntiForgeryToken")
    tok = s.cookies.get("X-XSRF-TOKEN")
    if not tok:
        raise http.Blocked("allianz: no X-XSRF-TOKEN cookie")
    r = http.request(s, "POST", f"{BASE}/webapi/api/Fund/GetFundAssets",
                     json={"FundID": fid}, headers={"X-XSRF-TOKEN": tok})
    return r.json()


def parse(raw: dict, cfg: dict) -> dict:
    from . import new_snapshot
    snap = new_snapshot(cfg, page_url(cfg["params"]))
    data = (raw.get("Entries") or {}).get("Data") or {}
    fa = data.get("FundAsset") or {}
    snap["nav_twd"] = num(fa.get("Aum"))
    snap["units"] = num(fa.get("Units"))
    snap["nav_per_unit"] = num(fa.get("Nav"))
    nav_date = parse_date(fa.get("NavDate"))
    pcf_date = parse_date(fa.get("PCFDate"))
    snap["nav_date"] = nav_date.isoformat() if nav_date else None
    snap["pcf_date"] = pcf_date.isoformat() if pcf_date else None
    snap["position_date"] = prev_weekday(pcf_date).isoformat() if pcf_date else None
    a = snap["assets"]
    for t in data.get("Table") or []:
        title = (t.get("TableTitle") or "").strip()
        rows = t.get("Rows") or []
        if title == "":
            for r in rows:
                if r and str(r[0]).strip() == "股票":
                    a["stock_twd"] = num(r[-1])
                elif r and str(r[0]).strip() not in ("",):
                    a["other_items"].append({"name": str(r[0]).strip(), "amount_twd": num(r[-1])})
        elif title.startswith("股票"):
            m = re.search(r"([\d.]+)\s*%", title)
            a["stock_pct_reported"] = float(m.group(1)) if m else None
            for r in rows:
                if len(r) < 5:
                    continue
                sym = symbols.parse(r[1])
                snap["holdings"].append({
                    "key": sym.key, "code_raw": r[1], "name": r[2], "market": sym.market,
                    "currency": sym.currency, "shares": num(r[3]), "weight": num(r[4]), "amount_twd": None,
                })
        elif "附買回" in title:
            for r in rows:
                amt = num(r[-1])
                a["rp_items"].append({"name": r[1] if len(r) > 2 else r[0], "amount_twd": amt})
                a["rp_twd"] += amt or 0.0
        elif "期貨" in title:
            for r in rows:
                a["futures"].append({"code": None, "name": " ".join(map(str, r[1:-1])), "side": None,
                                     "contracts": None, "notional_twd": num(r[-1]), "month": None, "raw": r})
        elif "現金" in title:
            for r in rows:
                a["cash_twd"] += num(r[-1]) or 0.0
        elif "債" in title:
            for r in rows:
                a["bond_twd"] += num(r[-1]) or 0.0
        else:
            snap["notes"].append(f"未辨識的表格：{title}")
    # Cash & others are not itemised by 安聯 → derived as residual in analytics.
    a["cash_is_residual"] = True
    return snap

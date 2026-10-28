"""Turn raw daily snapshots + prices into everything the dashboard shows.

Key definitions (shown to the user as tooltips too):
- 股票%            issuer-reported stock value / NAV.
- 現金及其他%       100 − 股票% − 債券%  (includes 附買回、現金、保證金、應收付；期貨名目不算資產).
- 有效股票曝險%     股票% + 期貨多單名目% − 期貨空單名目%.
- 申購贖回剔除      expected shares from flows = 前一日股數 × (今日單位數 / 前一日單位數);
                    主動調整股數 = 今日股數 − expected.  (Needs units on both days.)
- 推算平均成本      not disclosed by issuers; estimated: each day's share increase × that day's typical
                    price (H+L+C)/3, average-cost method on decreases. First tracked day uses that
                    day's price as the baseline. Always labelled 推算.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

from .prices import PriceStore
from .symbols import ASIA, MARKET_NAME

SPLIT_RATIOS = [2, 3, 4, 5, 10, 20, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10]
TINY_WEIGHT = 0.01   # % of NAV; below this a position is treated as 象徵性持股
UNCHANGED_TOL = 0.002  # |active change| / position below this = unchanged (rounding of flows)


def _r(x: float | None, n: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, n)


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return a / b * 100


# ----------------------------------------------------------------------------- per snapshot
def enrich(snap: dict, store: PriceStore, themes: dict) -> dict:
    """Attach prices, FX, market values, themes and ETF-level ratios to one snapshot."""
    date = snap["position_date"]
    nav = snap.get("nav_twd")
    a = snap["assets"]
    issuer_fx = a.get("fx") or {}
    hold = []
    missing_price = []
    for h in snap["holdings"]:
        t = store.ticker(h["key"])
        close, close_date = store.close_on(t, date) if date else (None, None)
        bar = store.bar_on(t, close_date) if close_date else None
        typical = None
        if bar and bar[1] is not None and bar[2] is not None:
            typical = (bar[0] + bar[1] + bar[2]) / 3
        latest, latest_time = store.latest(t)
        fx_c = issuer_fx.get(h["currency"]) or store.fx(h["currency"], date)
        fx_l = store.fx(h["currency"])
        amt = h.get("amount_twd")
        calc = h["shares"] * close * fx_c if (close is not None and fx_c and h.get("shares") is not None) else None
        if amt is None:
            amt = calc
        if close is None:
            missing_price.append(h["key"])
        w = h.get("weight")
        if w is None and amt is not None and nav:
            w = amt / nav * 100
        hold.append({
            **h, "weight": w, "ticker": t, "theme": themes.get(h["key"], "未分類"),
            "market_name": MARKET_NAME.get(h["market"], h["market"]),
            "close": _r(close), "close_date": close_date, "typical": _r(typical),
            "latest": _r(latest), "latest_time": latest_time,
            "fx_close": _r(fx_c, 6), "fx_latest": _r(fx_l, 6),
            "amount_twd": _r(amt, 0),
            "latest_amount_twd": _r(h["shares"] * latest * fx_l, 0) if (latest is not None and fx_l and h.get("shares")) else None,
            "tiny": (w is not None and w < TINY_WEIGHT),
        })
    hold.sort(key=lambda x: -(x["weight"] or 0))

    stock_pct = a.get("stock_pct_reported")
    if stock_pct is None:
        stock_pct = sum(h["weight"] or 0 for h in hold)
    bond_pct = pct(a.get("bond_twd"), nav) or 0.0
    rp_pct = pct(a.get("rp_twd"), nav) or 0.0
    cash_other = 100 - stock_pct - bond_pct
    fut_long = sum(f.get("notional_twd") or 0 for f in a.get("futures", []) if f.get("side") != "short")
    fut_short = sum(f.get("notional_twd") or 0 for f in a.get("futures", []) if f.get("side") == "short")
    fl, fs = pct(fut_long, nav) or 0.0, pct(fut_short, nav) or 0.0
    breakdown = {"股票": stock_pct, "債券": bond_pct}
    if a.get("cash_is_residual"):
        breakdown.update({"附買回債券": rp_pct, "現金及其他（推算）": cash_other - rp_pct})
    else:
        items = {
            "現金": pct(a.get("cash_twd"), nav), "期貨保證金": pct(a.get("futures_margin_twd"), nav),
            "附買回債券": rp_pct, "應收付證券款": pct(a.get("receivable_payable_twd"), nav),
            "應付贖回款": pct(a.get("redemption_payable_twd"), nav), "其他": pct(a.get("other_twd"), nav),
        }
        itemised = sum(v or 0 for v in items.values())
        breakdown.update({k: v for k, v in items.items() if v})
        resid = cash_other - itemised
        if abs(resid) > 0.05:
            breakdown["未分項差額"] = resid
    by_market: dict[str, float] = defaultdict(float)
    by_theme: dict[str, float] = defaultdict(float)
    for h in hold:
        by_market[h["market_name"]] += h["weight"] or 0
        by_theme[h["theme"]] += h["weight"] or 0
    w_all = sum(h["weight"] or 0 for h in hold) or 1e-9
    real = [h for h in hold if not h["tiny"]]
    metrics = {
        "nav_twd": nav, "units": snap.get("units"), "nav_per_unit": snap.get("nav_per_unit"),
        "stock_pct": _r(stock_pct), "bond_pct": _r(bond_pct), "rp_pct": _r(rp_pct),
        "cash_other_pct": _r(cash_other), "futures_long_pct": _r(fl), "futures_short_pct": _r(fs),
        "effective_equity_pct": _r(stock_pct + fl - fs),
        "n_holdings": len(real), "n_tiny": len(hold) - len(real),
        "top10_pct": _r(sum(h["weight"] or 0 for h in hold[:10])),
        "max_weight": _r(max((h["weight"] or 0) for h in hold) if hold else None),
        "asia_pct": _r(sum(h["weight"] or 0 for h in hold if h["market"] in ASIA)),
        "us_pct": _r(sum(h["weight"] or 0 for h in hold if h["market"] == "US")),
        "tw_share_of_stock": _r(sum(h["weight"] or 0 for h in hold if h["market"] == "TW") / w_all * 100),
        "foreign_pct": _r(sum(h["weight"] or 0 for h in hold if h["market"] != "TW")),
        "breakdown": {k: _r(v) for k, v in breakdown.items() if v and abs(v) > 1e-4},
        "by_market": {k: _r(v) for k, v in sorted(by_market.items(), key=lambda x: -x[1])},
        "by_theme": {k: _r(v) for k, v in sorted(by_theme.items(), key=lambda x: -x[1])},
        "missing_price": missing_price,
    }
    return {**{k: v for k, v in snap.items() if k != "holdings"}, "holdings": hold, "metrics": metrics}


# ----------------------------------------------------------------------------- changes
def _split_ratio(sp: float, sc: float, pp: float | None, pc: float | None) -> float | None:
    if not sp or not sc:
        return None
    r = sc / sp
    for cand in SPLIT_RATIOS:
        if abs(r / cand - 1) < 0.01 and abs(cand - 1) > 0.01:
            if pp and pc and abs((pc / pp) * cand - 1) < 0.25:
                return cand
    return None


def diff(prev: dict | None, cur: dict) -> dict:
    """Compare two enriched snapshots of the same ETF."""
    if prev is None:
        return {"date": cur["position_date"], "prev_date": None, "rows": [], "summary": {}, "flow_adjusted": False}
    up, uc = prev.get("units"), cur.get("units")
    unit_ratio = (uc / up) if (up and uc) else None
    P = {h["key"]: h for h in prev["holdings"]}
    C = {h["key"]: h for h in cur["holdings"]}
    rows = []
    nav = cur.get("nav_twd") or 0
    for k in sorted(set(P) | set(C)):
        p, c = P.get(k), C.get(k)
        sp = (p or {}).get("shares") or 0.0
        sc = (c or {}).get("shares") or 0.0
        ref = c or p
        price = (c or {}).get("close") or (p or {}).get("close")
        fx = (c or {}).get("fx_close") or (p or {}).get("fx_close") or 1.0
        ca = _split_ratio(sp, sc, (p or {}).get("close"), (c or {}).get("close"))
        expected = sp * unit_ratio if unit_ratio else sp
        total = sc - sp
        active = 0.0 if ca else sc - expected
        flow = 0.0 if ca else expected - sp
        if sp == 0 and sc > 0:
            status = "新增"
        elif sp > 0 and sc == 0:
            status = "剔除"
        elif ca:
            status = "公司行動"
        elif abs(active) <= max(sp, 1) * UNCHANGED_TOL:
            status = "不變" if total == 0 else "隨申贖等比例變動"
        else:
            status = "加碼" if active > 0 else "減碼"
        amt_active = active * price * fx if price else None
        rows.append({
            "key": k, "name": ref["name"], "market_name": ref.get("market_name"), "theme": ref.get("theme"),
            "shares_prev": sp, "shares_cur": sc, "delta_shares": total, "flow_shares": _r(flow, 2),
            "active_shares": _r(active, 2), "active_pct_of_pos": _r(active / sp * 100, 2) if sp else None,
            "weight_prev": (p or {}).get("weight"), "weight_cur": (c or {}).get("weight"),
            "delta_weight": _r(((c or {}).get("weight") or 0) - ((p or {}).get("weight") or 0)),
            "price": price, "active_amount_twd": _r(amt_active, 0),
            "active_pct_nav": _r(pct(amt_active, nav)) if amt_active is not None else None,
            "status": status, "split_ratio": ca,
        })
    order = {"新增": 0, "剔除": 1, "加碼": 2, "減碼": 3, "公司行動": 4, "隨申贖等比例變動": 5, "不變": 6}
    rows.sort(key=lambda r: (order[r["status"]], -abs(r["active_amount_twd"] or 0)))
    buys = sum(r["active_amount_twd"] or 0 for r in rows if (r["active_amount_twd"] or 0) > 0)
    sells = sum(r["active_amount_twd"] or 0 for r in rows if (r["active_amount_twd"] or 0) < 0)
    flow_twd = None
    if up and uc and cur.get("nav_per_unit"):
        flow_twd = (uc - up) * cur["nav_per_unit"]
    pm, cm = prev["metrics"], cur["metrics"]
    summary = {
        "n_new": sum(r["status"] == "新增" for r in rows), "n_exit": sum(r["status"] == "剔除" for r in rows),
        "n_add": sum(r["status"] == "加碼" for r in rows), "n_trim": sum(r["status"] == "減碼" for r in rows),
        "active_buy_twd": _r(buys, 0), "active_sell_twd": _r(sells, 0), "active_net_twd": _r(buys + sells, 0),
        "net_creation_twd": _r(flow_twd, 0), "unit_change": _r((uc - up) if (up and uc) else None, 0),
        "cash_other_change_pp": _r((cm["cash_other_pct"] or 0) - (pm["cash_other_pct"] or 0)),
        "stock_change_pp": _r((cm["stock_pct"] or 0) - (pm["stock_pct"] or 0)),
        "effective_change_pp": _r((cm["effective_equity_pct"] or 0) - (pm["effective_equity_pct"] or 0)),
    }
    by_theme: dict[str, float] = defaultdict(float)
    by_market: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["active_amount_twd"]:
            by_theme[r["theme"] or "未分類"] += r["active_amount_twd"]
            by_market[r["market_name"] or "?"] += r["active_amount_twd"]
    summary["active_by_theme"] = {k: _r(v, 0) for k, v in sorted(by_theme.items(), key=lambda x: -abs(x[1]))}
    summary["active_by_market"] = {k: _r(v, 0) for k, v in sorted(by_market.items(), key=lambda x: -abs(x[1]))}
    return {"date": cur["position_date"], "prev_date": prev["position_date"], "rows": rows,
            "summary": summary, "flow_adjusted": unit_ratio is not None, "unit_ratio": _r(unit_ratio, 6)}


# ----------------------------------------------------------------------------- cost estimate
def estimate_costs(enriched: list[dict], diffs: list[dict]) -> dict[str, dict]:
    """Average-cost estimate per holding across the ETF's tracked history. Returns latest state."""
    state: dict[str, dict] = {}
    for i, snap in enumerate(enriched):
        cur = {h["key"]: h for h in snap["holdings"]}
        d = diffs[i]
        drow = {r["key"]: r for r in d["rows"]}
        for k, h in cur.items():
            px = h.get("typical") or h.get("close")
            fx = h.get("fx_close") or 1.0
            sh = h.get("shares") or 0.0
            st = state.get(k)
            if st is None:
                st = state[k] = {"shares": 0.0, "cost_local": 0.0, "cost_twd": 0.0, "first_date": snap["position_date"],
                                 "basis": "起始基準" if i == 0 else "推算", "pending": 0.0, "buys": 0}
            prev_sh = st["shares"]
            r = drow.get(k)
            if r and r.get("split_ratio"):
                st["shares"] = sh
                continue
            delta = sh - prev_sh
            if delta > 0:
                if px:
                    st["cost_local"] += delta * px
                    st["cost_twd"] += delta * px * fx
                    st["buys"] += 1
                else:
                    st["pending"] += delta
            elif delta < 0 and prev_sh > 0:
                keep = sh / prev_sh
                st["cost_local"] *= keep
                st["cost_twd"] *= keep
                st["pending"] *= keep
            if st["pending"] and px:  # price became available: book pending shares at today's price
                st["cost_local"] += st["pending"] * px
                st["cost_twd"] += st["pending"] * px * fx
                st["pending"] = 0.0
                st["basis"] += "（含補價）"
            st["shares"] = sh
        for k in [k for k in state if k not in cur]:
            del state[k]
    out = {}
    for k, st in state.items():
        priced = st["shares"] - st["pending"]
        out[k] = {
            "avg_cost_local": _r(st["cost_local"] / priced, 4) if priced > 0 and st["cost_local"] else None,
            "avg_cost_twd": _r(st["cost_twd"] / priced, 4) if priced > 0 and st["cost_twd"] else None,
            "basis": st["basis"], "first_date": st["first_date"], "n_buy_days": st["buys"],
        }
    return out


# ----------------------------------------------------------------------------- ETF bundle
def build_etf(code: str, snaps: list[dict], store: PriceStore, themes: dict) -> dict:
    enriched = [enrich(s, store, themes) for s in snaps]
    diffs = [diff(enriched[i - 1] if i else None, e) for i, e in enumerate(enriched)]
    costs = estimate_costs(enriched, diffs)
    latest = enriched[-1]
    for h in latest["holdings"]:
        c = costs.get(h["key"], {})
        h.update(c)
        if c.get("avg_cost_local") and h.get("latest"):
            h["est_unrealized_pct"] = _r((h["latest"] / c["avg_cost_local"] - 1) * 100, 2)
    series = []
    for e, d in zip(enriched, diffs):
        m = e["metrics"]
        series.append({
            "date": e["position_date"], "nav_twd": m["nav_twd"], "units": m["units"], "nav_per_unit": m["nav_per_unit"],
            "stock_pct": m["stock_pct"], "cash_other_pct": m["cash_other_pct"], "rp_pct": m["rp_pct"],
            "futures_long_pct": m["futures_long_pct"], "effective_equity_pct": m["effective_equity_pct"],
            "n_holdings": m["n_holdings"], "top10_pct": m["top10_pct"], "foreign_pct": m["foreign_pct"],
            "by_market": m["by_market"], "breakdown": m["breakdown"],
            "active_net_twd": d["summary"].get("active_net_twd"), "net_creation_twd": d["summary"].get("net_creation_twd"),
        })
    # per-holding share history (for sparklines and the stock page)
    hist: dict[str, list] = defaultdict(list)
    for e in enriched:
        cur = {h["key"]: h for h in e["holdings"]}
        for k in set(cur) | set(hist):
            h = cur.get(k)
            hist[k].append([e["position_date"], h["shares"] if h else 0, _r(h["weight"]) if h else 0])
    return {"code": code, "latest": latest, "diffs": diffs, "series": series, "share_history": dict(hist),
            "enriched": enriched}


# ----------------------------------------------------------------------------- cross-fund
def aggregate(bundles: dict[str, dict]) -> dict:
    by_date: dict[str, dict[str, Any]] = defaultdict(dict)
    for code, b in bundles.items():
        for e, d in zip(b["enriched"], b["diffs"]):
            by_date[e["position_date"]][code] = (e, d)
    days = []
    for date in sorted(by_date):
        items = by_date[date]
        aum = sum((e["metrics"]["nav_twd"] or 0) for e, _ in items.values())

        def wavg(field):
            vals = [(e["metrics"][field], e["metrics"]["nav_twd"]) for e, _ in items.values()
                    if e["metrics"][field] is not None and e["metrics"]["nav_twd"]]
            w = sum(n for _, n in vals)
            if not w or len(vals) < len(items):
                return None  # skip days where some ETF lacks NAV (e.g. the 10/15 seed snapshot)
            return _r(sum(v * n for v, n in vals) / w)
        theme: dict[str, float] = defaultdict(float)
        market: dict[str, float] = defaultdict(float)
        buyers: dict[str, set] = defaultdict(set)
        sellers: dict[str, set] = defaultdict(set)
        names: dict[str, str] = {}
        n_add = n_trim = n_new = n_exit = funds_buy = funds_sell = 0
        net_total = 0.0
        has_diff = False
        for code, (e, d) in items.items():
            if not d["prev_date"]:
                continue
            has_diff = True
            s = d["summary"]
            n_add += s["n_add"]; n_trim += s["n_trim"]; n_new += s["n_new"]; n_exit += s["n_exit"]
            net = s["active_net_twd"] or 0
            net_total += net
            funds_buy += net > 0
            funds_sell += net < 0
            for k, v in s["active_by_theme"].items():
                theme[k] += v or 0
            for k, v in s["active_by_market"].items():
                market[k] += v or 0
            for r in d["rows"]:
                names[r["key"]] = r["name"]
                if r["status"] in ("新增", "加碼"):
                    buyers[r["key"]].add(code)
                elif r["status"] in ("剔除", "減碼"):
                    sellers[r["key"]].add(code)
        days.append({
            "date": date, "n_etfs": len(items), "etfs": sorted(items), "aum_twd": _r(aum, 0),
            "cash_other_pct": wavg("cash_other_pct"), "stock_pct": wavg("stock_pct"),
            "effective_equity_pct": wavg("effective_equity_pct"), "futures_long_pct": wavg("futures_long_pct"),
            "has_changes": has_diff, "active_net_twd": _r(net_total, 0) if has_diff else None,
            "n_add": n_add, "n_trim": n_trim, "n_new": n_new, "n_exit": n_exit,
            "breadth": _r((n_add + n_new - n_trim - n_exit) / max(1, n_add + n_new + n_trim + n_exit) * 100, 1) if has_diff else None,
            "funds_net_buy": funds_buy, "funds_net_sell": funds_sell,
            "active_by_theme": {k: _r(v, 0) for k, v in sorted(theme.items(), key=lambda x: -abs(x[1]))},
            "active_by_market": {k: _r(v, 0) for k, v in sorted(market.items(), key=lambda x: -abs(x[1]))},
            "consensus_buy": [{"key": k, "name": names[k], "etfs": sorted(v)} for k, v in buyers.items() if len(v) >= 2],
            "consensus_sell": [{"key": k, "name": names[k], "etfs": sorted(v)} for k, v in sellers.items() if len(v) >= 2],
        })
    # crowding on the latest snapshot of each ETF
    held: dict[str, dict] = {}
    for code, b in bundles.items():
        for h in b["latest"]["holdings"]:
            if h["tiny"]:
                continue
            x = held.setdefault(h["key"], {"key": h["key"], "name": h["name"], "theme": h["theme"],
                                           "market_name": h["market_name"], "etfs": {}, "amount_twd": 0.0})
            x["etfs"][code] = _r(h["weight"])
            x["amount_twd"] += h.get("amount_twd") or 0
    crowd = sorted([v for v in held.values() if len(v["etfs"]) >= 2], key=lambda v: (-len(v["etfs"]), -v["amount_twd"]))
    return {"days": days, "crowded": crowd}


# ----------------------------------------------------------------------------- rules
def check_rules(bundles: dict[str, dict], rules: dict) -> dict:
    out = {}
    for code, b in bundles.items():
        cfg = (rules.get("etfs") or {}).get(code)
        if not cfg:
            out[code] = {"checks": [], "rules": [], "note": "尚未建立公開說明書規則"}
            continue
        m = b["latest"]["metrics"]
        res = []
        for c in cfg.get("checks", []):
            if not c.get("metric"):
                res.append({**c, "value": None, "status": "未檢查", "margin": None})
                continue
            v = m.get(c["metric"])
            if v is None:
                res.append({**c, "value": None, "status": "資料不足", "margin": None})
                continue
            ok = v >= c["limit"] if c["op"] == ">=" else v <= c["limit"]
            margin = (v - c["limit"]) if c["op"] == ">=" else (c["limit"] - v)
            status = "符合" if ok else ("未達" if c["op"] == ">=" else "超過")
            note = None
            if ok and margin < c.get("warn_margin", 3):
                status = "接近邊界"
            if status == "超過" and c["id"] == "single_max":
                note = ("單一個股上限通常以買進時點計算，股價上漲造成的被動超過不一定違規；"
                        "主管機關 2025 年起對部分 ETF 放寬權值股上限。請以投信公告為準。")
            res.append({**c, "value": _r(v, 2), "status": status, "margin": _r(margin, 2), "note": note})
        out[code] = {"checks": res, "rules": cfg.get("rules", []), "name": cfg.get("name"),
                     "listing_date": cfg.get("listing_date"), "benchmark": cfg.get("benchmark")}
    return out


# ----------------------------------------------------------------------------- reports
def _period_key(date: str, kind: str) -> str:
    d = dt.date.fromisoformat(date)
    if kind == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{d.year}-{d.month:02d}"


def reports(bundles: dict[str, dict], agg: dict) -> dict:
    out = {"week": [], "month": []}
    for kind in ("week", "month"):
        periods: dict[str, dict] = {}
        for code, b in bundles.items():
            for e, d in zip(b["enriched"], b["diffs"]):
                pk = _period_key(e["position_date"], kind)
                P = periods.setdefault(pk, {"period": pk, "etfs": {}})
                E = P["etfs"].setdefault(code, {"start": None, "end": None, "net": 0.0, "stocks": defaultdict(float),
                                                "names": {}, "new": set(), "exit": set()})
                m = e["metrics"]
                if E["start"] is None:
                    E["start"] = {"date": e["position_date"], "cash_other_pct": m["cash_other_pct"], "stock_pct": m["stock_pct"]}
                E["end"] = {"date": e["position_date"], "cash_other_pct": m["cash_other_pct"], "stock_pct": m["stock_pct"]}
                for r in d["rows"]:
                    if r["active_amount_twd"]:
                        E["stocks"][r["key"]] += r["active_amount_twd"]
                        E["names"][r["key"]] = r["name"]
                        E["net"] += r["active_amount_twd"]
                    if r["status"] == "新增":
                        E["new"].add(r["name"])
                    elif r["status"] == "剔除":
                        E["exit"].add(r["name"])
        for pk in sorted(periods, reverse=True)[: (10 if kind == "week" else 8)]:
            P = periods[pk]
            etfs = {}
            lines = []
            for code, E in sorted(P["etfs"].items()):
                top = sorted(E["stocks"].items(), key=lambda x: -x[1])
                buys = [{"name": E["names"][k], "twd": _r(v, 0)} for k, v in top if v > 0][:5]
                sells = [{"name": E["names"][k], "twd": _r(v, 0)} for k, v in reversed(top) if v < 0][:5]
                dcash = None
                if E["start"]["cash_other_pct"] is not None and E["end"]["cash_other_pct"] is not None:
                    dcash = _r(E["end"]["cash_other_pct"] - E["start"]["cash_other_pct"], 2)
                etfs[code] = {"start": E["start"], "end": E["end"], "net_active_twd": _r(E["net"], 0),
                              "top_buys": buys, "top_sells": sells, "new": sorted(E["new"]), "exit": sorted(E["exit"]),
                              "cash_other_change_pp": dcash}
                txt = f"{code}：主動淨{'買' if E['net'] >= 0 else '賣'} {abs(E['net']) / 1e8:.2f} 億"
                if dcash is not None:
                    txt += f"，現金及其他 {E['end']['cash_other_pct']:.2f}%（{dcash:+.2f} 個百分點）"
                if buys:
                    txt += "；加碼最多：" + "、".join(b["name"] for b in buys[:3])
                if sells:
                    txt += "；減碼最多：" + "、".join(s["name"] for s in sells[:3])
                if E["new"]:
                    txt += "；新增：" + "、".join(sorted(E["new"])[:5])
                if E["exit"]:
                    txt += "；剔除：" + "、".join(sorted(E["exit"])[:5])
                lines.append(txt)
            out[kind].append({"period": pk, "etfs": etfs, "summary": lines})
    return out


# ----------------------------------------------------------------------------- backtest
def backtest(agg: dict, store: PriceStore, min_obs: int = 60) -> dict:
    idx = store.hist.get("^TWII", {}).get("d", {})
    days = sorted(idx)
    pos = {d: i for i, d in enumerate(days)}
    obs = []
    for a in agg["days"]:
        if a["cash_other_pct"] is None or a["date"] not in pos:
            continue
        i = pos[a["date"]]
        row = {"date": a["date"], "cash_other_pct": a["cash_other_pct"], "active_net_twd": a["active_net_twd"],
               "breadth": a["breadth"]}
        for h in (5, 20):
            if i + h < len(days):
                row[f"fwd{h}"] = _r((idx[days[i + h]][0] / idx[days[i]][0] - 1) * 100, 3)
        obs.append(row)
    res = {"n_obs": len(obs), "min_obs": min_obs, "status": "樣本不足" if len(obs) < min_obs else "可計算",
           "observations": obs[-250:], "correlations": {}}
    if len(obs) >= min_obs:
        for x in ("cash_other_pct", "breadth"):
            for h in (5, 20):
                pairs = [(o[x], o.get(f"fwd{h}")) for o in obs if o.get(x) is not None and o.get(f"fwd{h}") is not None]
                if len(pairs) >= min_obs // 2:
                    xs, ys = zip(*pairs)
                    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                    cov = sum((a - mx) * (b - my) for a, b in pairs)
                    vx = sum((a - mx) ** 2 for a in xs)
                    vy = sum((b - my) ** 2 for b in ys)
                    res["correlations"][f"{x}→未來{h}日加權指數報酬"] = {
                        "n": len(pairs), "corr": _r(cov / math.sqrt(vx * vy), 3) if vx and vy else None}
    return res

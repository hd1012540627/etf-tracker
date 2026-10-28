"""Entry point.

  python -m tracker.run --mode full      # 抓持股 + 股價 + 重算 + 輸出網站資料（每日 09:00 排程）
  python -m tracker.run --mode prices    # 只更新股價後重算（手動一鍵更新的快速模式）
  python -m tracker.run --mode rebuild   # 不連網，只重算
  python -m tracker.run --mode full --browser   # 用無頭瀏覽器抓（被網站擋時）
  python -m tracker.run --demo           # 用開發時擷取的真實資料（tests/fixtures）離線產生示範資料

Exit code 3 = 至少一檔 ETF 抓取被網站擋下（workflow 會改用瀏覽器模式重跑）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import traceback

from . import alerts as alerts_mod
from . import analytics, http
from .adapters import ADAPTERS
from .prices import PriceStore, load_fixture
from .util import (DATA, RAW_DIR, ROOT, SITE_DATA, SNAP_DIR, load_config, now_tpe, read_json, setup_logging,
                   write_json)

log = logging.getLogger("tracker")


def snapshot_paths(code: str) -> list:
    d = SNAP_DIR / code
    return sorted(d.glob("*.json")) if d.exists() else []


def load_snaps(code: str) -> list[dict]:
    return [read_json(p) for p in snapshot_paths(code)]


def _digest(snap: dict) -> str:
    core = {"h": [(h["key"], h["shares"]) for h in snap["holdings"]], "nav": snap.get("nav_twd"), "u": snap.get("units"),
            "a": {k: v for k, v in snap["assets"].items() if k not in ("fx",)}}
    return hashlib.sha1(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def validate(snap: dict) -> list[str]:
    problems = []
    if not snap.get("holdings"):
        problems.append("沒有持股資料")
    if not snap.get("position_date"):
        problems.append("沒有持股日期")
    if not snap.get("nav_twd"):
        problems.append("沒有淨資產")
    return problems


def save_snapshot(snap: dict) -> str:
    """Store snapshot under data/snapshots/<etf>/<position_date>.json. Returns 'new'|'updated'|'same'."""
    snap["digest"] = _digest(snap)
    path = SNAP_DIR / snap["etf"] / f"{snap['position_date']}.json"
    old = read_json(path)
    if old and old.get("digest") == snap["digest"]:
        return "same"
    write_json(path, snap)
    return "updated" if old else "new"


def fetch_all(etfs: list[dict], runlog: dict) -> bool:
    blocked = False
    for cfg in etfs:
        code = cfg["code"]
        mod = ADAPTERS[cfg["adapter"]]
        rec = {"etf": code, "time": now_tpe().isoformat(timespec="seconds"),
               "mode": "browser" if http.browser_enabled() else "requests"}
        try:
            raw = mod.fetch_raw(cfg["params"])
            write_json(RAW_DIR / f"{code}_latest.json", raw)
            snap = mod.parse(raw, cfg)
            snap["fetched_at"] = rec["time"]
            probs = validate(snap)
            if probs:
                raise ValueError("；".join(probs))
            rec["result"] = save_snapshot(snap)
            rec["position_date"] = snap["position_date"]
            rec["n_holdings"] = len(snap["holdings"])
            rec["ok"] = True
            log.info("%s %s %s holdings=%d", code, snap["position_date"], rec["result"], len(snap["holdings"]))
        except http.Blocked as e:
            blocked = True
            rec.update(ok=False, error=f"被網站阻擋：{e}")
            log.warning("%s blocked: %s", code, e)
        except Exception as e:  # noqa: BLE001
            rec.update(ok=False, error=f"{type(e).__name__}: {e}")
            log.error("%s failed: %s\n%s", code, e, traceback.format_exc())
        runlog.setdefault("fetch", []).append(rec)
    return blocked


def health_report(bundles: dict, store: PriceStore, runs: list[dict], etfs: list[dict]) -> dict:
    days = store.trading_days()
    last_trading = days[-1] if days else None
    out = {"generated_at": now_tpe().isoformat(timespec="seconds"), "last_trading_day": last_trading,
           "price_errors": store.errors[:50], "price_diag": getattr(store, "diag", [])[:20], "etfs": {},
           "runs": [{"start": r.get("start"), "end": r.get("end"), "mode": r.get("mode"), "trigger": r.get("trigger"),
                     "browser": r.get("browser"),
                     "ok": sum(1 for f in r.get("fetch", []) if f.get("ok")),
                     "fail": [f"{f['etf']}：{f.get('error')}" for f in r.get("fetch", []) if not f.get("ok")],
                     "price_errors": len(r.get("price_errors") or [])} for r in runs[-12:]][::-1]}
    for cfg in etfs:
        code = cfg["code"]
        b = bundles.get(code)
        recent = [r for run in runs[-20:] for r in run.get("fetch", []) if r["etf"] == code]
        last = recent[-1] if recent else None
        last_ok = next((r for r in reversed(recent) if r.get("ok")), None)
        h = {"name": cfg.get("name"), "issuer": cfg.get("issuer"), "source": cfg.get("url"),
             "last_attempt": last and last["time"], "last_success": last_ok and last_ok["time"],
             "last_error": None if (not last or last.get("ok")) else last.get("error"),
             "fetch_mode": last and last.get("mode")}
        if b:
            m = b["latest"]["metrics"]
            pdate = b["latest"]["position_date"]
            stale = None
            if last_trading and pdate:
                stale = sum(1 for d in days if pdate < d <= last_trading)
                # PCF-style issuers publish the evening after; one day lag is normal before ~18:00
            wsum = sum(x["weight"] or 0 for x in b["latest"]["holdings"])
            h.update({
                "position_date": pdate, "pcf_date": b["latest"].get("pcf_date"), "nav_date": b["latest"].get("nav_date"),
                "stale_days": stale, "n_snapshots": len(b["enriched"]), "n_rows": len(b["latest"]["holdings"]),
                "weight_sum": round(wsum, 2), "stock_pct": m["stock_pct"],
                "weight_gap": round(wsum - (m["stock_pct"] or 0), 2),
                "missing_price": m["missing_price"],
                "unclassified": [x["name"] for x in b["latest"]["holdings"] if x["theme"] == "未分類"],
                "notes": b["latest"].get("notes", []),
            })
        out["etfs"][code] = h
    return out


def build_site(etfs: list[dict], store: PriceStore, runs: list[dict]) -> None:
    themes = (load_config("themes.json") or {}).get("themes", {})
    rules = load_config("rules.json") or {}
    alert_cfg = load_config("alerts.json") or {}
    bundles = {}
    for cfg in etfs:
        snaps = load_snaps(cfg["code"])
        if snaps:
            bundles[cfg["code"]] = analytics.build_etf(cfg["code"], snaps, store, themes)
    agg = analytics.aggregate(bundles)
    rules_status = analytics.check_rules(bundles, rules)
    health = health_report(bundles, store, runs, etfs)
    alerts = alerts_mod.build_alerts(bundles, rules_status, health, alert_cfg)
    reps = analytics.reports(bundles, agg)
    bt = analytics.backtest(agg, store)

    SITE_DATA.mkdir(parents=True, exist_ok=True)
    meta = {"generated_at": now_tpe().isoformat(timespec="seconds"), "etfs": [
        {k: c.get(k) for k in ("code", "name", "issuer", "url")} for c in etfs],
        "repo": os.environ.get("GITHUB_REPOSITORY"), "workflow": "daily.yml",
        "demo": os.environ.get("TRACKER_DEMO") == "1"}
    write_json(SITE_DATA / "meta.json", meta)
    for code, b in bundles.items():
        lt = b["latest"]
        write_json(SITE_DATA / f"etf_{code}.json", {
            "code": code, "latest": lt, "series": b["series"],
            "changes": [{k: v for k, v in d.items()} for d in b["diffs"][-30:]][::-1],
            "share_history": b["share_history"],
        }, compact=True)
    # stock index
    stocks: dict[str, dict] = {}
    for code, b in bundles.items():
        for h in b["latest"]["holdings"]:
            s = stocks.setdefault(h["key"], {"key": h["key"], "name": h["name"], "market_name": h["market_name"],
                                             "theme": h["theme"], "ticker": h["ticker"], "latest": h["latest"],
                                             "latest_time": h["latest_time"], "currency": h["currency"], "holders": {}})
            last = next((r for r in b["diffs"][-1]["rows"] if r["key"] == h["key"]), None)
            s["holders"][code] = {"shares": h["shares"], "weight": h["weight"], "amount_twd": h["amount_twd"],
                                  "avg_cost_local": h.get("avg_cost_local"), "basis": h.get("basis"),
                                  "first_date": h.get("first_date"), "last_status": last and last["status"],
                                  "last_active_shares": last and last["active_shares"],
                                  "history": b["share_history"].get(h["key"], [])}
        for r in b["diffs"][-1]["rows"]:
            if r["status"] == "剔除":
                s = stocks.setdefault(r["key"], {"key": r["key"], "name": r["name"], "market_name": r["market_name"],
                                                 "theme": r["theme"], "holders": {}})
                s.setdefault("exited", []).append(code)
    write_json(SITE_DATA / "stocks.json", stocks, compact=True)
    write_json(SITE_DATA / "aggregate.json", agg, compact=True)
    write_json(SITE_DATA / "rules.json", rules_status)
    write_json(SITE_DATA / "health.json", health)
    write_json(SITE_DATA / "reports.json", reps)
    write_json(SITE_DATA / "backtest.json", bt)
    idx = store.hist.get("^TWII", {}).get("d", {})
    write_json(SITE_DATA / "market.json", {"taiex": [[d, v[0]] for d, v in sorted(idx.items())]}, compact=True)
    # alerts history (dedupe by etf/date/kind/msg)
    hist = read_json(DATA / "alerts_history.json", []) or []
    seen = {(a["etf"], a["date"], a["kind"], a["msg"]) for a in hist}
    fresh = [a for a in alerts if (a["etf"], a["date"], a["kind"], a["msg"]) not in seen]
    stamp = now_tpe().isoformat(timespec="seconds")
    for a in fresh:
        a["first_seen"] = stamp
    hist = (hist + fresh)[-500:]
    write_json(DATA / "alerts_history.json", hist)
    write_json(SITE_DATA / "alerts.json", {"current": alerts, "history": hist[::-1][:300]})
    if fresh and os.environ.get("TRACKER_NOTIFY") == "1":
        site = os.environ.get("SITE_URL")
        sent = alerts_mod.notify(fresh, alert_cfg, site)
        log.info("notifications sent via %s", sent)


def seed_demo() -> PriceStore:
    """Offline demo using real data captured on 2026-10-28 (see tests/fixtures/README)."""
    from .adapters import allianz, capitalfund, ezmoney
    fx = ROOT / "tests" / "fixtures"
    etfs = {c["code"]: c for c in load_config("etfs.json")["etfs"]}
    s1 = allianz.parse(read_json(fx / "allianz_E0004_20261029.json"), etfs["00412A"])
    s2 = ezmoney.parse(ezmoney.fixture_to_raw(open(fx / "ezmoney_49YTW_20261028.txt", encoding="utf-8").readlines()),
                       etfs["00981A"])
    s3 = capitalfund.parse(read_json(fx / "capitalfund_502_20261029.json"), etfs["00997A"])
    seed = read_json(fx / "allianz_E0004_20261015_seed.json")
    for s in (seed, s1, s2, s3):
        if s:
            s.setdefault("fetched_at", "2026-10-28T23:30:00+08:00")
            if not s.get("fetched_at"):
                s["fetched_at"] = "2026-10-28T23:30:00+08:00"
            save_snapshot(s)
    store = PriceStore()
    load_fixture(store, fx / "yahoo_prices_20261028.txt")
    return store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "prices", "rebuild"], default="full")
    ap.add_argument("--browser", action="store_true", help="用無頭瀏覽器抓取")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    setup_logging(a.v)
    if a.browser:
        os.environ["TRACKER_BROWSER"] = "1"
    etfs = load_config("etfs.json")["etfs"]
    runs = read_json(DATA / "runs.json", []) or []
    runlog = {"start": now_tpe().isoformat(timespec="seconds"), "mode": a.mode, "browser": a.browser,
              "trigger": os.environ.get("GITHUB_EVENT_NAME", "local")}
    blocked = False
    if a.demo:
        os.environ["TRACKER_DEMO"] = "1"
        store = seed_demo()
        runlog["fetch"] = [{"etf": c["code"], "time": "2026-10-28T23:30:00+08:00", "ok": True, "mode": "開發時擷取",
                            "result": "demo"} for c in etfs]
    else:
        if a.mode == "full":
            blocked = fetch_all(etfs, runlog)
        store = PriceStore()
        if a.mode in ("full", "prices"):
            keys = set()
            for c in etfs:
                for s in load_snaps(c["code"])[-25:]:
                    keys |= {h["key"] for h in s["holdings"]}
            store.refresh(keys, rng="3mo" if not store.hist else "1mo")
            store.save()
            runlog["price_errors"] = store.errors[:30]
            runlog["price_diag"] = store.diag[:20]
    runlog["end"] = now_tpe().isoformat(timespec="seconds")
    runs = (runs + [runlog])[-200:]
    write_json(DATA / "runs.json", runs)
    build_site(etfs, store, runs)
    http.close_browser()
    return 3 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())

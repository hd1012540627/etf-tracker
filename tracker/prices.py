"""Prices and FX.

Primary source: Yahoo Finance chart API (free, used by the open-source yfinance package).
  https://query1.finance.yahoo.com/v8/finance/chart/<ticker>?range=1mo&interval=1d
Verified 2026-10-28 for TW (.TW/.TWO), JP (.T), KR (.KS), CN (.SS/.SZ), US tickers, FX (TWD=X ...)
and ^TWII.

Cross-check (optional, best effort): TWSE / TPEx open data daily closes.

Store: data/prices/history.json  {ticker: {"ccy": "TWD", "d": {date: [close, high, low]}}}
       data/prices/live.json     {ticker: {"price": x, "time": iso, "ccy": "USD"}}
       data/prices/symbols.json  {key: ticker}  (resolved .TW / .TWO etc.)
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import logging
from typing import Iterable

from . import http, symbols
from .util import PRICE_DIR, TPE, read_json, write_json

log = logging.getLogger("tracker.prices")

YAHOO = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
INDEX = "^TWII"
KEEP_DAYS = 800


class PriceStore:
    def __init__(self):
        self.hist: dict = read_json(PRICE_DIR / "history.json", {}) or {}
        self.live: dict = read_json(PRICE_DIR / "live.json", {}) or {}
        self.symmap: dict = read_json(PRICE_DIR / "symbols.json", {}) or {}
        self.errors: list[str] = []

    # ----------------------------------------------------------------- lookups
    def ticker(self, key: str) -> str | None:
        if key in self.symmap:
            return self.symmap[key]
        if key.startswith("TW:"):
            return None  # resolved (.TW or .TWO) on first refresh
        return symbols.parse(self._raw_from_key(key)).yahoo

    def close_on(self, ticker: str | None, date: str, max_back: int = 6) -> tuple[float | None, str | None]:
        """Close on `date` or the closest earlier trading date (within max_back days)."""
        if not ticker or ticker not in self.hist:
            return None, None
        d = self.hist[ticker]["d"]
        day = dt.date.fromisoformat(date)
        for i in range(max_back + 1):
            k = (day - dt.timedelta(days=i)).isoformat()
            if k in d and d[k][0] is not None:
                return d[k][0], k
        return None, None

    def bar_on(self, ticker: str | None, date: str) -> list | None:
        if not ticker or ticker not in self.hist:
            return None
        return self.hist[ticker]["d"].get(date)

    def latest(self, ticker: str | None) -> tuple[float | None, str | None]:
        if not ticker:
            return None, None
        lv = self.live.get(ticker)
        if lv and lv.get("price") is not None:
            return lv["price"], lv.get("time")
        if ticker in self.hist and self.hist[ticker]["d"]:
            k = max(self.hist[ticker]["d"])
            return self.hist[ticker]["d"][k][0], k
        return None, None

    def fx(self, ccy: str, date: str | None = None) -> float | None:
        if ccy == "TWD":
            return 1.0
        t = symbols.FX_TICKERS.get(ccy)
        if not t:
            return None
        if date:
            v, _ = self.close_on(t, date, max_back=7)
            if v:
                return v
        v, _ = self.latest(t)
        return v

    def trading_days(self) -> list[str]:
        if INDEX in self.hist:
            return sorted(self.hist[INDEX]["d"])
        return []

    # ----------------------------------------------------------------- fetching
    def _yahoo(self, sess, ticker: str, rng: str) -> dict | None:
        for base in YAHOO:
            try:
                r = sess.get(f"{base}/v8/finance/chart/{ticker}", params={"range": rng, "interval": "1d"}, timeout=20)
                if r.status_code != 200:
                    continue
                res = (r.json().get("chart") or {}).get("result")
                if not res:
                    return None
                return res[0]
            except Exception as e:  # noqa: BLE001
                log.debug("yahoo %s %s: %s", ticker, base, e)
        return None

    def _ingest(self, ticker: str, res: dict) -> None:
        meta = res.get("meta") or {}
        off = meta.get("gmtoffset") or 0
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        ts = res.get("timestamp") or []
        rec = self.hist.setdefault(ticker, {"ccy": meta.get("currency"), "d": {}})
        rec["ccy"] = meta.get("currency") or rec.get("ccy")
        for i, t in enumerate(ts):
            c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
            if c is None:
                continue
            vol = (q.get("volume") or [None] * len(ts))[i]
            day = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date().isoformat()
            h = (q.get("high") or [None] * len(ts))[i]
            lo = (q.get("low") or [None] * len(ts))[i]
            rec["d"][day] = [round(c, 4), None if h is None else round(h, 4), None if lo is None else round(lo, 4),
                             vol]
        price = meta.get("regularMarketPrice")
        if price is not None:
            t = meta.get("regularMarketTime")
            self.live[ticker] = {
                "price": round(float(price), 4), "ccy": meta.get("currency"),
                "time": dt.datetime.fromtimestamp(t, TPE).isoformat() if t else None,
            }

    def refresh(self, keys: Iterable[str], rng: str = "1mo") -> None:
        keys = sorted(set(keys))
        sess = http.session()
        tickers: dict[str, list[str]] = {}
        for k in keys:
            if k in self.symmap:
                tickers[k] = [self.symmap[k]]
            elif k.startswith("TW:"):
                c = k[3:]
                tickers[k] = [f"{c}.TW", f"{c}.TWO"]
            else:
                s = symbols.parse(self._raw_from_key(k))
                tickers[k] = [s.yahoo] if s.yahoo else []
        extra = [INDEX] + list(symbols.FX_TICKERS.values())

        def work(item):
            k, cands = item
            for t in cands:
                res = self._yahoo(sess, t, rng)
                if res and res.get("timestamp"):
                    return k, t, res
            return k, None, None

        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for k, t, res in ex.map(work, list(tickers.items()) + [(x, [x]) for x in extra]):
                if t is None:
                    if k not in extra:
                        self.errors.append(f"找不到股價：{k}")
                    continue
                if k not in extra:
                    self.symmap[k] = t
                self._ingest(t, res)
        self._clean_tw_holidays()
        self._trim()

    @staticmethod
    def _raw_from_key(key: str) -> str:
        m, c = key.split(":", 1)
        suffix = {"US": "US", "JP": "JP", "KR": "KS", "CN": "C2" if c.startswith(("0", "3")) else "C1", "HK": "HK"}.get(m)
        return f"{c} {suffix}" if suffix else c

    def _clean_tw_holidays(self) -> None:
        """Yahoo sometimes repeats the previous close on Taiwan holidays. Keep only dates on which
        the TAIEX (^TWII) traded."""
        days = set(self.trading_days())
        if not days:
            return
        lo, hi = min(days), max(days)
        for t, rec in self.hist.items():
            if t.endswith((".TW", ".TWO")):
                for k in [k for k in rec["d"] if k not in days and lo <= k < hi]:
                    del rec["d"][k]

    def _trim(self) -> None:
        cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
        for rec in self.hist.values():
            for k in [k for k in rec["d"] if k < cutoff]:
                del rec["d"][k]

    def save(self) -> None:
        write_json(PRICE_DIR / "history.json", self.hist, compact=True)
        write_json(PRICE_DIR / "live.json", self.live)
        write_json(PRICE_DIR / "symbols.json", self.symmap)


def load_fixture(store: PriceStore, path) -> None:
    """Load the pipe fixture captured during development (tests/fixtures/yahoo_prices_*.txt)."""
    for ln in open(path, encoding="utf-8"):
        t, ccy, pts = ln.rstrip("\n").split("~")
        rec = store.hist.setdefault(t, {"ccy": ccy, "d": {}})
        for p in pts.split(","):
            f = p.split(":")
            day = "2026-" + f[0]
            c = float(f[1])
            h = float(f[2]) if len(f) > 2 else None
            lo = float(f[3]) if len(f) > 3 else None
            rec["d"][day] = [c, h, lo, None]
        last = max(rec["d"])
        store.live[t] = {"price": rec["d"][last][0], "ccy": ccy, "time": last + "T23:00:00+08:00"}
        if not t.startswith("^") and not t.endswith("=X"):
            if t.endswith((".TW", ".TWO")):
                store.symmap["TW:" + t.split(".")[0]] = t
            else:
                base, _, suf = t.partition(".")
                m = {"T": "JP", "KS": "KR", "KQ": "KR", "SS": "CN", "SZ": "CN", "HK": "HK"}.get(suf, "US")
                store.symmap[f"{m}:{base}"] = t
    store._clean_tw_holidays()

"""Normalise issuer stock codes into a canonical key, market, currency and Yahoo ticker.

Canonical key: '<MARKET>:<code>' e.g. TW:2330, JP:6857, US:AVGO, KR:000660, CN:002463.
Issuer formats seen (2026-10):
  安聯: '2330', '6857 JP', 'AVGO US', '000660 KS', '002463 C2' (深股通), '600183 C1' (滬股通)
  統一: '2330'
  群益: '2330', 'CRWD US', '4062 JP', '009150 KS'
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SUFFIX = {
    "JP": ("JP", "JPY", "{c}.T"),
    "JT": ("JP", "JPY", "{c}.T"),
    "KS": ("KR", "KRW", "{c}.KS"),
    "KQ": ("KR", "KRW", "{c}.KQ"),
    "US": ("US", "USD", "{c}"),
    "UW": ("US", "USD", "{c}"),
    "UN": ("US", "USD", "{c}"),
    "C1": ("CN", "CNY", "{c}.SS"),   # 滬股通 (Shanghai)
    "CH": ("CN", "CNY", "{c}.SS"),
    "C2": ("CN", "CNY", "{c}.SZ"),   # 深股通 (Shenzhen)
    "HK": ("HK", "HKD", "{c}.HK"),
    "TT": ("TW", "TWD", "{c}.TW"),
}

MARKET_NAME = {"TW": "台灣", "US": "美國", "JP": "日本", "KR": "韓國", "CN": "中國A股", "HK": "香港"}
ASIA = {"TW", "JP", "KR", "CN", "HK"}


@dataclass(frozen=True)
class Sym:
    key: str        # TW:2330
    code: str       # 2330
    market: str     # TW
    currency: str   # TWD
    yahoo: str | None  # 2330.TW (None for TW until resolved .TW/.TWO)


def parse(raw: str) -> Sym:
    raw = (raw or "").strip().upper()
    parts = raw.split()
    if len(parts) >= 2 and parts[-1] in SUFFIX:
        code = parts[0]
        market, ccy, tpl = SUFFIX[parts[-1]]
        if market == "CN" and parts[-1] in ("C1", "CH") and code.startswith(("0", "3")):
            tpl = "{c}.SZ"  # defensive: Shenzhen codes start with 0/3
        return Sym(f"{market}:{code}", code, market, ccy, tpl.format(c=code))
    m = re.fullmatch(r"(\d{4,6}[A-Z]?)(?:\.(TW|TWO))?", raw)
    if m:
        return Sym(f"TW:{m.group(1)}", m.group(1), "TW", "TWD", None)
    # unknown format: treat as US ticker when alphabetic
    if re.fullmatch(r"[A-Z.\-]{1,6}", raw):
        return Sym(f"US:{raw}", raw, "US", "USD", raw)
    return Sym(f"XX:{raw}", raw, "XX", "TWD", None)


FX_TICKERS = {"USD": "TWD=X", "JPY": "JPYTWD=X", "KRW": "KRWTWD=X", "CNY": "CNYTWD=X", "HKD": "HKDTWD=X"}

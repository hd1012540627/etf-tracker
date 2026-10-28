"""Issuer adapters. Each module exposes fetch_raw(params) and parse(raw, etf_cfg) -> snapshot dict."""
from . import allianz, capitalfund, ezmoney

ADAPTERS = {"allianz": allianz, "ezmoney": ezmoney, "capitalfund": capitalfund}


def new_snapshot(cfg: dict, source_url: str) -> dict:
    return {
        "etf": cfg["code"], "name": cfg.get("name"), "issuer": cfg.get("issuer"),
        "source_url": source_url, "fetched_at": None,
        "position_date": None, "pcf_date": None, "nav_date": None,
        "nav_twd": None, "units": None, "nav_per_unit": None,
        "holdings": [],
        "assets": {"stock_twd": None, "stock_pct_reported": None, "rp_twd": 0.0, "rp_items": [],
                   "cash_twd": 0.0, "futures_margin_twd": 0.0, "receivable_payable_twd": 0.0,
                   "redemption_payable_twd": 0.0, "bond_twd": 0.0, "other_twd": 0.0,
                   "other_items": [], "futures": [], "fx": {}},
        "notes": [],
    }

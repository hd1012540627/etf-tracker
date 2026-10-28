"""Shared helpers: paths, JSON IO, dates, logging."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("TRACKER_ROOT", Path(__file__).resolve().parents[1]))
CONFIG = ROOT / "config"
DATA = Path(os.environ.get("TRACKER_DATA", ROOT / "data"))
SNAP_DIR = DATA / "snapshots"
PRICE_DIR = DATA / "prices"
RAW_DIR = DATA / "raw"
SITE_DATA = Path(os.environ.get("TRACKER_SITE_DATA", ROOT / "site" / "data"))

TPE = dt.timezone(dt.timedelta(hours=8))

log = logging.getLogger("tracker")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def now_tpe() -> dt.datetime:
    return dt.datetime.now(TPE)


def today_tpe() -> dt.date:
    return now_tpe().date()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(path: Path, obj: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        if compact:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def load_config(name: str) -> Any:
    return read_json(CONFIG / name)


def parse_date(s: str | None) -> dt.date | None:
    """Accept 2026/10/27, 2026-10-27, 2026-10-27T00:00:00, '2026/10/29 上午 12:00:00'."""
    if not s:
        return None
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(s))
    if not m:
        return None
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def num(s: Any) -> float | None:
    """Parse '6,372,377,867', 'TWD$6,372,377,867', '7.58%', 'USD 5,925,106.13' -> float."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^0-9.\-]", "", str(s))
    if t in ("", "-", ".", "-."):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def prev_weekday(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d

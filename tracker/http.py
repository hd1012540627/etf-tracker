"""HTTP layer: plain requests first; optional headless-browser fallback (Playwright).

Some issuer sites sit behind bot protection (Imperva/F5). When a plain request is
blocked we load the issuer page in headless Chromium and run the same API call
inside the page, exactly like a normal visitor's browser does.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger("tracker.http")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


class Blocked(Exception):
    """Response looked like a bot-protection page or failed after retries."""


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    return s


def looks_blocked(resp: requests.Response) -> bool:
    if resp.status_code in (401, 403, 429, 503):
        return True
    ct = resp.headers.get("content-type", "")
    body = resp.text[:2000].lower()
    return ("incapsula" in body or "_incapsula_resource" in body or "request unsuccessful" in body
            or ("text/html" in ct and "captcha" in body))


def request(sess: requests.Session, method: str, url: str, retries: int = 3, **kw) -> requests.Response:
    last: Exception | None = None
    for i in range(retries):
        try:
            r = sess.request(method, url, timeout=30, **kw)
            if looks_blocked(r):
                raise Blocked(f"{url} -> HTTP {r.status_code} (blocked?)")
            r.raise_for_status()
            return r
        except Blocked:
            raise
        except Exception as e:  # network hiccup
            last = e
            time.sleep(2 * (i + 1))
    raise Blocked(f"{url} failed after {retries} tries: {last}")


# ---------------------------------------------------------------- browser mode
_pw = None
_browser = None


def browser_enabled() -> bool:
    return os.environ.get("TRACKER_BROWSER", "0") == "1"


def _ensure_browser():
    global _pw, _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright  # lazy import
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
    return _browser


def browser_eval(page_url: str, js: str, wait_ms: int = 4000) -> Any:
    """Open page_url in headless Chromium, wait, then evaluate `js` (an async arrow fn body
    returning JSON-serialisable data)."""
    b = _ensure_browser()
    ctx = b.new_context(user_agent=UA, locale="zh-TW")
    page = ctx.new_page()
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        return page.evaluate(js)
    finally:
        ctx.close()


def close_browser() -> None:
    global _pw, _browser
    if _browser is not None:
        _browser.close()
        _pw.stop()
        _browser = _pw = None


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)

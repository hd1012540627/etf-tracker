"""Alerts (shown on the dashboard) and optional push notifications.

Notifications are OFF unless the matching GitHub Actions secrets exist:
  Email: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_EMAIL_TO
  LINE Messaging API: LINE_CHANNEL_TOKEN, LINE_USER_ID
Thresholds live in config/alerts.json.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import smtplib
from email.mime.text import MIMEText

import requests

log = logging.getLogger("tracker.alerts")

DEFAULTS = {
    "active_trade_pct_nav": 0.3,     # 單日主動買賣金額 ≥ 淨值 0.3%
    "weight_change_pp": 0.5,         # 權重變化 ≥ 0.5 個百分點（含股價影響）
    "cash_change_pp": 2.0,           # 現金及其他比例單日變化 ≥ 2 個百分點
    "rule_margin_pp": 3.0,           # 距離公開說明書下限/上限 < 3 個百分點
    "stale_trading_days": 2,         # 持股日落後 ≥ 2 個交易日
    "notify_levels": ["高"],          # 只推播這些等級
}


def build_alerts(bundles: dict, rules_status: dict, health: dict, cfg: dict) -> list[dict]:
    c = {**DEFAULTS, **(cfg or {})}
    out: list[dict] = []
    for code, b in bundles.items():
        d = b["diffs"][-1]
        date = b["latest"]["position_date"]
        if d["prev_date"]:
            for r in d["rows"]:
                if r["status"] == "新增":
                    out.append(_a(code, date, "高", "新增持股", f"{r['name']}（{r['key']}）新進場，權重 {_f(r['weight_cur'])}%"))
                elif r["status"] == "剔除":
                    out.append(_a(code, date, "高", "剔除持股", f"{r['name']}（{r['key']}）全數出清，前一日權重 {_f(r['weight_prev'])}%"))
                elif r["status"] in ("加碼", "減碼") and (abs(r["active_pct_nav"] or 0) >= c["active_trade_pct_nav"]):
                    out.append(_a(code, date, "中", f"大額{r['status']}",
                                  f"{r['name']} 主動{r['status']} {r['active_shares']:+,.0f} 股，約 {abs(r['active_amount_twd'] or 0) / 1e8:.2f} 億（淨值 {abs(r['active_pct_nav']):.2f}%）"))
                elif abs(r["delta_weight"] or 0) >= c["weight_change_pp"] and r["status"] != "公司行動":
                    out.append(_a(code, date, "低", "權重大變動",
                                  f"{r['name']} 權重 {_f(r['weight_prev'])}% → {_f(r['weight_cur'])}%（含股價漲跌影響）"))
                elif r["status"] == "公司行動":
                    out.append(_a(code, date, "中", "疑似公司行動", f"{r['name']} 股數變為 {r['split_ratio']} 倍，判定為分割/合併，不計入買賣"))
            ch = d["summary"].get("cash_other_change_pp")
            if ch is not None and abs(ch) >= c["cash_change_pp"]:
                out.append(_a(code, date, "高", "現金部位大變動",
                              f"現金及其他比例變動 {ch:+.2f} 個百分點，目前 {_f(b['latest']['metrics']['cash_other_pct'])}%"))
        for chk in (rules_status.get(code) or {}).get("checks", []):
            if chk["status"] in ("未達", "超過"):
                out.append(_a(code, date, "高", f"公開說明書規則{chk['status']}",
                              f"{chk['label']}：目前 {chk['value']}" + (f"（{chk['note']}）" if chk.get("note") else "")))
            elif chk["status"] == "接近邊界":
                out.append(_a(code, date, "中", "接近規則邊界", f"{chk['label']}：目前 {chk['value']}，距離 {chk['margin']} 個百分點"))
    for code, h in (health.get("etfs") or {}).items():
        if h.get("stale_days") is not None and h["stale_days"] >= c["stale_trading_days"]:
            out.append(_a(code, h.get("position_date"), "高", "資料過期", f"持股日 {h.get('position_date')} 已落後 {h['stale_days']} 個交易日"))
        if h.get("last_error"):
            out.append(_a(code, h.get("position_date"), "高", "抓取失敗", h["last_error"]))
        if h.get("missing_price"):
            out.append(_a(code, h.get("position_date"), "低", "缺股價", "、".join(h["missing_price"][:8])))
    return out


def _f(x) -> str:
    return "—" if x is None else f"{x:.2f}"


def _a(etf, date, level, kind, msg):
    return {"etf": etf, "date": date, "level": level, "kind": kind, "msg": msg}


def notify(alerts: list[dict], cfg: dict, site_url: str | None = None) -> list[str]:
    c = {**DEFAULTS, **(cfg or {})}
    pick = [a for a in alerts if a["level"] in c["notify_levels"]]
    if not pick:
        return []
    text = "主動 ETF 追蹤器提醒 " + dt.date.today().isoformat() + "\n" + "\n".join(
        f"[{a['level']}] {a['etf']} {a['kind']}：{a['msg']}" for a in pick[:30])
    if site_url:
        text += f"\n\n儀表板：{site_url}"
    sent = []
    if os.environ.get("SMTP_HOST") and os.environ.get("ALERT_EMAIL_TO"):
        try:
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = f"主動ETF追蹤器：{len(pick)} 則提醒"
            msg["From"] = os.environ.get("SMTP_USER", "")
            msg["To"] = os.environ["ALERT_EMAIL_TO"]
            with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "465"))) as s:
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
            sent.append("email")
        except Exception as e:  # noqa: BLE001
            log.warning("email failed: %s", e)
    if os.environ.get("LINE_CHANNEL_TOKEN") and os.environ.get("LINE_USER_ID"):
        try:
            r = requests.post("https://api.line.me/v2/bot/message/push", timeout=20,
                              headers={"Authorization": f"Bearer {os.environ['LINE_CHANNEL_TOKEN']}",
                                       "Content-Type": "application/json"},
                              data=json.dumps({"to": os.environ["LINE_USER_ID"],
                                               "messages": [{"type": "text", "text": text[:4900]}]}))
            r.raise_for_status()
            sent.append("line")
        except Exception as e:  # noqa: BLE001
            log.warning("line failed: %s", e)
    return sent

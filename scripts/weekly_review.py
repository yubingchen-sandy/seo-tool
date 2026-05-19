"""Weekly keyword tuning review.

Reads docs/all.json, finds:
  • PROMOTIONS — related keywords appearing on >= PROMOTION_MIN_DAYS distinct
    dates within the trailing WINDOW_DAYS, NOT already in monitored_keywords.
    These are stable signals worth promoting to T3 seeds.
  • DEMOTIONS — monitored_keywords that produced zero rising rows in the
    trailing window. Suggested for review (they may be too narrow / niche).

Posts a plain-text Lark message via LARK_WEBHOOK_URL.

Env vars:
  LARK_WEBHOOK_URL     required
  DASHBOARD_URL        optional, defaults to the GitHub Pages URL
  WINDOW_DAYS          optional, default 14
  PROMOTION_MIN_DAYS   optional, default 2 (raise once dataset matures)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALL_PATH = REPO_ROOT / "docs" / "all.json"

WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "14"))
PROMOTION_MIN_DAYS = int(os.environ.get("PROMOTION_MIN_DAYS", "2"))
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "https://yubingchen-sandy.github.io/seo-tool/"
)
TOP_PROMOTIONS = 10
TOP_DEMOTIONS = 10


def load_data() -> dict:
    if not ALL_PATH.exists():
        print("docs/all.json not found — nothing to review.", file=sys.stderr)
        sys.exit(0)
    return json.loads(ALL_PATH.read_text(encoding="utf-8"))


def in_window(date_str: str, today, window_days: int) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return 0 <= (today - d).days < window_days


def compute_review(data: dict) -> dict:
    today = datetime.now(timezone.utc).date()
    rows = data.get("rows", [])
    window_rows = [r for r in rows if in_window(r.get("Date", ""), today, WINDOW_DAYS)]
    monitored_original = list(data.get("monitored_keywords") or [])
    monitored_lower = {k.lower().strip() for k in monitored_original}

    # ---- Promotions --------------------------------------------------------
    # Aggregate window rows by lowercased related keyword.
    by_kw: dict[str, dict] = defaultdict(lambda: {
        "dates": set(), "regions": set(),
        "max_value": 0, "max_trend": "",
        "display": "", "source_keyword": "",
    })
    for r in window_rows:
        rk = (r.get("Related Keyword") or "").strip()
        if not rk:
            continue
        if rk.lower() in monitored_lower:
            continue
        entry = by_kw[rk.lower()]
        entry["display"] = entry["display"] or rk
        entry["dates"].add(r.get("Date"))
        entry["regions"].add(r.get("Region"))
        v = r.get("Value") or 0
        if v > entry["max_value"]:
            entry["max_value"] = v
            entry["max_trend"] = r.get("Trend", "")
            entry["source_keyword"] = r.get("Keyword", "")
    promotions = sorted(
        (e for e in by_kw.values() if len(e["dates"]) >= PROMOTION_MIN_DAYS),
        key=lambda e: (len(e["dates"]), e["max_value"]),
        reverse=True,
    )[:TOP_PROMOTIONS]

    # ---- Demotions ---------------------------------------------------------
    produced = {r.get("Keyword") for r in window_rows if r.get("Keyword")}
    demotions = [k for k in monitored_original if k not in produced][:TOP_DEMOTIONS]

    return {
        "today": today.isoformat(),
        "window_days": WINDOW_DAYS,
        "promotion_min_days": PROMOTION_MIN_DAYS,
        "window_total_rows": len(window_rows),
        "window_total_dates": len({r.get("Date") for r in window_rows}),
        "monitored_count": len(monitored_original),
        "produced_count": len(produced),
        "promotions": promotions,
        "demotions": demotions,
    }


def build_message(review: dict) -> str:
    L = []
    # Leading line MUST contain "Google" for Lark's custom-keyword check.
    L.append("📊 Google Trends · 监控词每周调优建议")
    L.append("")
    L.append(
        f"窗口: 最近 {review['window_days']} 天 "
        f"({review['window_total_rows']} 条上升词 / {review['window_total_dates']} 天)"
    )
    L.append(
        f"监控词: {review['monitored_count']} 个 · "
        f"{review['produced_count']} 个有产出"
    )
    L.append("")

    # Promotions section
    promos = review["promotions"]
    if promos:
        L.append(
            f"🔥 建议晋升 ({review['promotion_min_days']}+ 天重复 · 未在监控列表)"
        )
        for p in promos:
            days = len(p["dates"])
            regions = "/".join(sorted(p["regions"]))
            tr = p["max_trend"] or "—"
            kw = f" ← from `{p['source_keyword']}`" if p["source_keyword"] else ""
            L.append(f"  • {p['display']} — {days}天 · {regions} · 峰值 {tr}{kw}")
    else:
        L.append(
            f"🔥 暂无晋升候选（{review['window_days']} 天内没有相关词重复出现"
            f" {review['promotion_min_days']} 次以上）"
        )

    L.append("")

    # Demotions section
    dems = review["demotions"]
    if dems:
        L.append(f"❄️ 建议下线（{review['window_days']} 天 0 产出）")
        for k in dems:
            L.append(f"  • {k}")
        L.append("  （冷词可能因数据期短而暂时未触发，建议观察 30 天后再下线）")
    else:
        L.append(f"❄️ 所有监控词都至少有 1 条上升记录")

    L.append("")
    L.append(f"👉 看板: {DASHBOARD_URL}")
    L.append("说明: 编辑 keywords.yml 的 brands / industry 列表，下次 cron 自动生效")
    return "\n".join(L)


def post_lark(webhook: str, text: str) -> None:
    payload = {"msg_type": "text", "content": {"text": text}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        print(f"Lark response {resp.status}: {body}")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        if parsed.get("code") not in (0, None):
            raise RuntimeError(f"Lark API error: {body}")


def main() -> int:
    data = load_data()
    review = compute_review(data)
    message = build_message(review)
    print("--- message ---")
    print(message)
    print("--- end message ---")
    webhook = os.environ.get("LARK_WEBHOOK_URL", "").strip()
    if not webhook:
        print("LARK_WEBHOOK_URL not set — printing only.", file=sys.stderr)
        return 0
    post_lark(webhook, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())

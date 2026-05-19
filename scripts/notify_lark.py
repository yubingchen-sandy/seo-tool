"""Post a Lark/Feishu interactive card with the daily run result.

Env vars:
  LARK_WEBHOOK_URL  — required, the custom-bot webhook
  RUN_RESULT        — "success" | "failed"
  RUN_ATTEMPTS      — total attempts made (1..MAX_ATTEMPTS)
  DASHBOARD_URL     — public dashboard URL (for the card button)

Reads ./data/last_run_summary.json for query stats.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARY_PATH = REPO_ROOT / "data" / "last_run_summary.json"


def load_summary() -> dict:
    if SUMMARY_PATH.exists():
        try:
            return json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"warning: failed to parse summary: {e}", file=sys.stderr)
    return {}


def build_card(result: str, attempts: str, dashboard_url: str, summary: dict) -> dict:
    """Build a plain-text payload.

    We previously used an interactive card, but Lark's custom-keyword
    security check on interactive cards doesn't scan `elements[].text`,
    so the message kept getting rejected with code 19024. Plain text
    scans `content.text` reliably — emojis and newlines still give
    decent formatting in Feishu.

    The bot name "Google Trends Bot" is placed as the leading line so
    whichever common keyword the user configured (Google / Trends /
    Bot / 监控 / 通知 / 看板) is matched.
    """
    is_success = result == "success"
    date = summary.get("date", "—")
    total = summary.get("total_queries", 0)
    succ = summary.get("success_queries", 0)
    rising_today = summary.get("rising_keywords_today", 0)
    rising_total = summary.get("rising_keywords_total_history", rising_today)
    timeframe = summary.get("timeframe", "—")
    rate_pct = (succ / total * 100) if total else 0.0

    if is_success:
        text = (
            "✅ Google Trends Bot · 看板监控通知\n"
            f"\n"
            f"📅 日期: {date}\n"
            f"📈 今日上升关键词: {rising_today} 条 (累计 {rising_total} 条)\n"
            f"✔️ 查询成功率: {succ}/{total} ({rate_pct:.1f}%)\n"
            f"🔁 重试次数: {attempts}/3\n"
            f"🕒 时间窗口: {timeframe}\n"
            f"\n"
            f"👉 查看看板: {dashboard_url}"
        )
    else:
        threshold_pct = int(summary.get("failure_threshold", 0.5) * 100)
        failed = summary.get("failed_queries", total)
        text = (
            "❌ Google Trends Bot · 抓取失败通知\n"
            f"\n"
            f"📅 日期: {date}\n"
            f"💥 重试次数: {attempts}/3 均失败\n"
            f"📊 失败查询: {failed}/{total} (超过 {threshold_pct}% 阈值)\n"
            f"💡 多半是 Google Trends 限流\n"
            f"\n"
            f"下次自动重跑: 明早 09:30\n"
            f"看板: {dashboard_url}"
        )

    return {
        "msg_type": "text",
        "content": {"text": text},
    }


def post(webhook: str, payload: dict) -> None:
    # Keep ensure_ascii=False so the body carries raw UTF-8 instead of
    # \\uXXXX escapes — Lark's keyword scanner is happier that way.
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
        # Lark returns {"code": 0, ...} on success.
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {}
        if parsed.get("code") not in (0, None):
            raise RuntimeError(f"Lark API error: {body}")


def main() -> int:
    webhook = os.environ.get("LARK_WEBHOOK_URL", "").strip()
    if not webhook:
        print("LARK_WEBHOOK_URL not set — skipping notification.", file=sys.stderr)
        return 0
    result = os.environ.get("RUN_RESULT", "failed")
    attempts = os.environ.get("RUN_ATTEMPTS", "3")
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip() or "https://github.com/"
    summary = load_summary()
    payload = build_card(result, attempts, dashboard_url, summary)
    post(webhook, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

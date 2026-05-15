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
    is_success = result == "success"
    date = summary.get("date", "—")
    total = summary.get("total_queries", 0)
    succ = summary.get("success_queries", 0)
    rising_today = summary.get("rising_keywords_today", 0)
    timeframe = summary.get("timeframe", "—")
    rate_pct = (succ / total * 100) if total else 0.0

    if is_success:
        header_title = "✅ Google Trends 看板已更新"
        header_template = "green"
        body = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**日期**\n{date}"}},
            {"is_short": True, "text": {"tag": "lark_md",
                                         "content": f"**重试次数**\n{attempts}/3"}},
            {"is_short": True, "text": {"tag": "lark_md",
                                         "content": f"**今日上升关键词**\n**{rising_today}** 条"}},
            {"is_short": True, "text": {"tag": "lark_md",
                                         "content": f"**查询成功率**\n{succ}/{total} ({rate_pct:.1f}%)"}},
        ]
        note = f"时间窗口: `{timeframe}`"
    else:
        header_title = "❌ Google Trends 抓取失败"
        header_template = "red"
        threshold_pct = int(summary.get("failure_threshold", 0.5) * 100)
        failed = summary.get("failed_queries", total)
        body = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**日期**\n{date}"}},
            {"is_short": True, "text": {"tag": "lark_md",
                                         "content": f"**重试次数**\n{attempts}/3 均失败"}},
            {"is_short": False, "text": {"tag": "lark_md",
                                          "content": (
                                              f"**原因**: {failed}/{total} 个查询无响应"
                                              f"（超过 {threshold_pct}% 失败阈值，多半是 Google Trends 限流）"
                                          )}},
        ]
        note = "下次自动重跑：明早 09:30"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": header_template,
            },
            "elements": [
                {"tag": "div", "fields": body},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看看板"},
                            "url": dashboard_url,
                            "type": "primary",
                        }
                    ],
                },
            ],
        },
    }


def post(webhook: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
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

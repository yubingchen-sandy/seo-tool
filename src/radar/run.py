"""Trend Radar daily run.

  python -m src.radar.run                # full run (needs ANTHROPIC_API_KEY; Lark optional)
  python -m src.radar.run --no-llm       # gather + score only (local smoke test)
  python -m src.radar.run --no-lark      # skip Base write + webhook card

Writes
  data/radar/daily/<date>.json   candidates + shortlist + stats (archive)
  data/radar/queued.json         rolling list of queued topics (dedupe memory)
  docs/radar/latest.json         what the dashboard page reads
and, when configured, appends the shortlist to the Lark Base review queue and
posts a text card to LARK_WEBHOOK_URL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from .gather import Gatherer, load_yaml, CONFIG_PATH, REDLINES_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RADAR_DATA = REPO_ROOT / "data" / "radar"
RADAR_DAILY = RADAR_DATA / "daily"
QUEUED = RADAR_DATA / "queued.json"
DOCS_LATEST = REPO_ROOT / "docs" / "radar" / "latest.json"

log = logging.getLogger("radar.run")


def load_queued(days: int) -> list[dict]:
    if not QUEUED.exists():
        return []
    try:
        rows = json.loads(QUEUED.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return [r for r in rows if r.get("date", "") >= since]


def build_card(date: str, shortlist: list[dict], stats: dict, dashboard_url: str, base_url: str) -> str:
    icon = {"new_page": "🆕", "update_existing": "♻️", "watch": "👀"}
    lines = [f"📡 Trend Radar Bot · 每日热点选题 {date}", "",
             f"信号 {stats.get('raw_items', 0)} 条 → 候选 {stats.get('n_candidates', 0)} → 入队 {len(shortlist)} 条"
             f"（新建 {sum(1 for v in shortlist if v['decision']=='new_page')} / 更新 "
             f"{sum(1 for v in shortlist if v['decision']=='update_existing')} / 观察 "
             f"{sum(1 for v in shortlist if v['decision']=='watch')}）", ""]
    for v in shortlist[:10]:
        lines.append(f"{icon.get(v['decision'], '•')} [{v['intent_type']}] {v.get('topic_en') or v['candidate']['title']}")
        lines.append(f"   ↳ {v.get('bridge', '')[:90]}")
    if len(shortlist) > 10:
        lines.append(f"… 另有 {len(shortlist) - 10} 条见队列")
    if stats.get("source_errors"):
        lines += ["", f"⚠️ 抓取失败来源: {', '.join(stats['source_errors'][:6])}"]
    lines += [""]
    if base_url:
        lines.append(f"📝 审核队列: {base_url}")
    lines.append(f"📊 看板: {dashboard_url}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-lark", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg, red = load_yaml(CONFIG_PATH), load_yaml(REDLINES_PATH)
    out = Gatherer(cfg, red).run()
    date = out["date"]
    cands = out["candidates"]
    stats = {"raw_items": out["raw_items"], "clusters": out["clusters"], "n_candidates": len(cands),
             "source_stats": out["source_stats"], "source_errors": out["source_errors"]}

    shortlist: list[dict] = []
    llm_info: dict = {}
    if not args.no_llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY not set — skipping classification (use --no-llm to silence)")
        else:
            from .classify import classify
            lookback = int(cfg.get("llm", {}).get("queued_lookback_days", 21))
            queued_titles = [r["topic"] for r in load_queued(lookback)]
            shortlist, llm_info = classify(cands, queued_titles, cfg, red)

    # ---- persist -----------------------------------------------------------
    RADAR_DAILY.mkdir(parents=True, exist_ok=True)
    DOCS_LATEST.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"date": date, "generated_at": out["generated_at"], "stats": stats, "llm": llm_info,
                "shortlist": shortlist, "candidates": cands[:150]}
    (RADAR_DAILY / f"{date}.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    DOCS_LATEST.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    if shortlist:
        prev = [r for r in load_queued(60) if r.get("date") != date]
        prev += [{"date": date, "topic": v.get("topic_en") or v["candidate"]["title"], "decision": v["decision"],
                  "intent_type": v["intent_type"]} for v in shortlist]
        QUEUED.write_text(json.dumps(prev, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("saved %s (%d candidates, %d shortlisted)", RADAR_DAILY / f"{date}.json", len(cands), len(shortlist))

    # ---- Lark ----------------------------------------------------------------
    if args.no_lark or not shortlist:
        return 0
    base_url = os.environ.get("LARK_BASE_URL", "").strip()
    try:
        from .lark_base import LarkBase
        lb = LarkBase()
        if lb.configured:
            lb.append(date, shortlist)
        else:
            log.info("Lark Base not configured — skipped")
    except Exception as e:  # noqa: BLE001
        log.error("Lark Base write failed: %s", e)
    webhook = os.environ.get("LARK_WEBHOOK_URL", "").strip()
    if webhook:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from notify_lark import post  # reuse the existing plain-text poster
        dashboard = os.environ.get("DASHBOARD_URL", "").rstrip("/") + "/radar.html"
        try:
            post(webhook, {"msg_type": "text", "content": {"text": build_card(date, shortlist, stats, dashboard, base_url)}})
        except Exception as e:  # noqa: BLE001
            log.error("Lark card failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())

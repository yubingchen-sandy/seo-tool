"""Daily Google Trends monitor.

Reads keywords.yml, queries Google Trends for each (keyword x region) pair,
keeps rising related queries above the configured % threshold, and writes
the results to data/ and docs/ for the dashboard.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import yaml
from pytrends.exceptions import ResponseError, TooManyRequestsError
from pytrends.request import TrendReq

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "keywords.yml"
DATA_DIR = REPO_ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
HISTORY_CSV = DATA_DIR / "history.csv"
DOCS_ALL = REPO_ROOT / "docs" / "all.json"
RUN_SUMMARY = REPO_ROOT / "data" / "last_run_summary.json"

# A run is considered failed if more than this fraction of queries got no
# response from Google Trends after the per-query retries inside
# fetch_rising(). Empty data (Google responded with no rising queries) does
# NOT count as failure — quiet days are legitimate.
FAILURE_RATIO_THRESHOLD = 0.5

# pytrends encodes "Breakout" as a sentinel integer well above any real %.
BREAKOUT_THRESHOLD = 100_000

# A (Keyword, Related Keyword) pair appearing on this many distinct dates
# gets flagged as "Recurring" in the dashboard — used as a 🔥 signal.
RECURRING_MIN_DAYS = 2

# ---- Category classification dictionaries -------------------------------
# Substring match (case-insensitive). Order of checks matters: see classify().
BRAND_TOKENS = {
    "meshy", "tripo", "sketchfab", "luma ai", "luma", "rodin", "csm ai",
    "spline", "kaedim", "alpha3d", "polycam", "scenario", "3dfy",
    "masterpiece studio", "tinkercad", "fusion 360", "shapeways",
    "thingiverse", "myminifactory", "thangs", "panzoid", "alpha 3d",
}
TOOL_TOKENS = {
    "blender", "blenderkit", "unity", "unreal", "maya", "cinema 4d",
    "3ds max", "zbrush", "houdini", "substance painter", "substance designer",
    "photoshop", "after effects", "meshmixer", "cg trader", "cgtrader",
    "asset store", "marketplace", "slicer", "slicing", "fusion",
    "rhino", "solidworks", "freecad", "openscad", "prusaslicer", "cura",
    "bambu", "creality", "ams",
}
FEATURE_PATTERNS = (
    "image to 3d", "text to 3d", "ai 3d", "3d generator", "3d model generator",
    "convert to 3d", "generator", "scan", "scanner", "to 3d model",
    "software", "online", "free", "best",
)
QUESTION_STARTS = (
    "what ", "how ", "why ", "when ", "where ", "which ",
    "who ", "is ", "are ", "does ", "do ", "can ", "should ",
    "will ", "wie ", "qu’est", "qu'est",
)


def classify(related_kw: str) -> str:
    """Return one of: Brand / Tool / Feature / Info / IP / Other.

    Cheap rules — no LLM. Categories drive the dashboard's SEO playbook.
    """
    s = (related_kw or "").lower().strip()
    if not s:
        return "Other"
    # 1) Question / informational has the cleanest signal — check first.
    if s.startswith(QUESTION_STARTS) or " vs " in s:
        return "Info"
    # 2) Brand match — direct competitor or recognized 3D platform.
    if any(b in s for b in BRAND_TOKENS):
        return "Brand"
    # 3) Adjacent tool / ecosystem.
    if any(t in s for t in TOOL_TOKENS):
        return "Tool"
    # 4) Generic feature / intent phrasing.
    if any(p in s for p in FEATURE_PATTERNS):
        return "Feature"
    # 5) Anything left in this niche is almost always IP / content driven
    # (character / movie / weapon / vehicle 3d model). Default there.
    return "IP"


def annotate_rows(rows: list[dict]) -> None:
    """Mutate rows in place: add `Category` and `Recurring` fields.

    Recurring = same (Keyword, Related Keyword) appears on >= RECURRING_MIN_DAYS
    distinct dates anywhere in the dataset (sticky signal vs one-off spike).
    """
    from collections import defaultdict
    pair_dates: dict[tuple, set] = defaultdict(set)
    for r in rows:
        pair_dates[(r.get("Keyword"), r.get("Related Keyword"))].add(r.get("Date"))
    for r in rows:
        r["Category"] = classify(r.get("Related Keyword", ""))
        r["Recurring"] = (
            len(pair_dates[(r.get("Keyword"), r.get("Related Keyword"))])
            >= RECURRING_MIN_DAYS
        )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("monitor")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_value(raw) -> tuple[int | None, str]:
    """Return (numeric_value_or_None, display_label).

    pytrends returns ints for rising queries and a huge sentinel for Breakout.
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None, str(raw)
    if n >= BREAKOUT_THRESHOLD:
        return n, "Breakout"
    return n, f"+{n}%"


def build_trends_link(query: str, geo: str, timeframe: str) -> str:
    base = "https://trends.google.com/trends/explore"
    # Google Trends accepts the same timeframe tokens pytrends uses.
    date_param = timeframe.replace(" ", "%20")
    params = f"?q={quote(query)}&date={date_param}"
    if geo:
        params += f"&geo={geo}"
    return base + params


def fetch_rising(
    pytrends: TrendReq,
    keyword: str,
    geo: str,
    timeframe: str,
    threshold: int,
    retries: int = 3,
) -> list[dict] | None:
    """Fetch rising related queries for one keyword/region.

    Returns a (possibly empty) list when Google responded; ``None`` when
    every retry failed to get a response (treated as a real failure by the
    caller).
    """
    for attempt in range(retries):
        try:
            pytrends.build_payload([keyword], timeframe=timeframe, geo=geo)
            related = pytrends.related_queries()
            block = related.get(keyword) or {}
            rising = block.get("rising")
            if rising is None or rising.empty:
                return []
            results = []
            for _, row in rising.iterrows():
                raw_value, label = normalize_value(row["value"])
                if raw_value is None:
                    continue
                if raw_value < threshold:
                    continue
                results.append(
                    {
                        "related_query": str(row["query"]),
                        "value": raw_value,
                        "trend_label": label,
                    }
                )
            return results
        except TooManyRequestsError:
            wait = 30 * (attempt + 1) + random.uniform(0, 10)
            log.warning("429 rate limit on %s/%s — sleeping %.1fs", keyword, geo or "WORLD", wait)
            time.sleep(wait)
        except ResponseError as e:
            log.warning("ResponseError on %s/%s: %s", keyword, geo or "WORLD", e)
            time.sleep(5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            log.error("Unexpected error on %s/%s: %s", keyword, geo or "WORLD", e)
            time.sleep(5)
    log.error("Gave up on %s/%s after %d retries", keyword, geo or "WORLD", retries)
    return None


def main() -> int:
    cfg = load_config()
    keywords = list(cfg.get("brands", [])) + list(cfg.get("industry", []))
    regions = cfg.get("regions", [])
    threshold = int(cfg.get("threshold", 500))
    timeframe = cfg.get("timeframe", "now 7-d")

    if not keywords or not regions:
        log.error("keywords.yml is missing keywords or regions")
        return 1

    DATA_DIR.mkdir(exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_ALL.parent.mkdir(parents=True, exist_ok=True)

    pytrends = TrendReq(
        hl="en-US",
        tz=0,
        timeout=(10, 30),
        retries=2,
        backoff_factor=0.5,
    )

    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    captured_at = now_utc.isoformat()

    rows: list[dict] = []
    total = len(keywords) * len(regions)
    failed_queries = 0
    i = 0
    for kw in keywords:
        for region in regions:
            i += 1
            geo = region.get("code", "")
            geo_name = region.get("name", geo or "Global")
            log.info("[%d/%d] %s @ %s", i, total, kw, geo_name)
            hits = fetch_rising(pytrends, kw, geo, timeframe, threshold)
            if hits is None:
                failed_queries += 1
            else:
                for hit in hits:
                    rows.append(
                        {
                            "Keyword": kw,
                            "Region": geo_name,
                            "Region Code": geo or "WORLD",
                            "Date": today,
                            "Related Keyword": hit["related_query"],
                            "Trend": hit["trend_label"],
                            "Value": hit["value"],
                            "Trend Type": "Rising",
                            "Source": build_trends_link(hit["related_query"], geo, timeframe),
                            "Captured At": captured_at,
                        }
                    )
            # be polite — pytrends is unofficial and Google rate-limits hard
            time.sleep(random.uniform(2.5, 4.5))

    success_queries = total - failed_queries
    failure_ratio = failed_queries / total if total else 0.0
    log.info(
        "Collected %d rows across %d/%d successful queries (%.1f%% failure)",
        len(rows), success_queries, total, failure_ratio * 100,
    )

    # --- merge with previous all.json -------------------------------------
    # If a row already exists for today's date in all.json we replace it —
    # so manual re-runs on the same day overwrite that day cleanly.
    previous_rows: list[dict] = []
    if DOCS_ALL.exists():
        try:
            previous_rows = json.loads(DOCS_ALL.read_text(encoding="utf-8")).get("rows", [])
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to read existing all.json: %s", e)
    today_dates = {today}
    merged_rows = [r for r in previous_rows if r.get("Date") not in today_dates] + rows
    # Backfill Category + Recurring across the full history each run, so
    # rule changes propagate to old rows on the next deploy.
    annotate_rows(merged_rows)
    # Sort newest first, then by Value desc for nicer default view.
    merged_rows.sort(
        key=lambda r: (r.get("Date") or "", r.get("Value") or 0),
        reverse=True,
    )
    available_dates = sorted({r["Date"] for r in merged_rows if r.get("Date")}, reverse=True)

    # Full configured scope so the dashboard's filter dropdowns can list
    # every monitored keyword/region, including ones that have not yet
    # produced any rising data above the threshold.
    monitored_keywords = list(keywords)
    monitored_regions = [r.get("name", r.get("code") or "Global") for r in regions]

    # --- write outputs ----------------------------------------------------
    snapshot_all = {
        "generated_at": captured_at,
        "latest_date": today,
        "available_dates": available_dates,
        "monitored_keywords": monitored_keywords,
        "monitored_regions": monitored_regions,
        "threshold": threshold,
        "timeframe": timeframe,
        "total": len(merged_rows),
        "today_total": len(rows),
        "rows": merged_rows,
    }
    DOCS_ALL.write_text(json.dumps(snapshot_all, ensure_ascii=False, indent=2), encoding="utf-8")
    # Per-day archive (today only)
    (DAILY_DIR / f"{today}.json").write_text(
        json.dumps(
            {
                "generated_at": captured_at,
                "date": today,
                "threshold": threshold,
                "timeframe": timeframe,
                "total": len(rows),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if rows:
        df = pd.DataFrame(rows)
        if HISTORY_CSV.exists():
            df.to_csv(HISTORY_CSV, mode="a", header=False, index=False)
        else:
            df.to_csv(HISTORY_CSV, index=False)
    elif not HISTORY_CSV.exists():
        pd.DataFrame(
            columns=[
                "Keyword", "Region", "Region Code", "Date", "Related Keyword",
                "Trend", "Value", "Trend Type", "Source", "Captured At",
            ]
        ).to_csv(HISTORY_CSV, index=False)

    # Clean up the pre-migration single-day file so it does not get stale.
    legacy_latest = REPO_ROOT / "docs" / "latest.json"
    if legacy_latest.exists():
        legacy_latest.unlink()

    # Write a machine-readable summary the workflow uses to build the Lark
    # notification. Always written, even on failure, so the notifier has
    # something to report.
    run_failed = failure_ratio > FAILURE_RATIO_THRESHOLD
    RUN_SUMMARY.write_text(
        json.dumps(
            {
                "success": not run_failed,
                "date": today,
                "captured_at": captured_at,
                "total_queries": total,
                "failed_queries": failed_queries,
                "success_queries": success_queries,
                "failure_ratio": round(failure_ratio, 4),
                "rising_keywords_today": len(rows),
                "rising_keywords_total_history": len(merged_rows),
                "timeframe": timeframe,
                "threshold": threshold,
                "failure_threshold": FAILURE_RATIO_THRESHOLD,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info("Wrote %s (%d total rows, %d dates)", DOCS_ALL, len(merged_rows), len(available_dates))

    if run_failed:
        log.error(
            "Run failed: %.1f%% query failure exceeds %.0f%% threshold",
            failure_ratio * 100, FAILURE_RATIO_THRESHOLD * 100,
        )
        return 2  # distinct exit code so workflow can tell it apart
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Trend Radar stage 1-3: gather signals, cluster, pre-score, sitemap overlap.

Sources (all optional except the first three; a failing source is logged and skipped):
  news          Google News RSS, one intent-shaped query per row of radar.yml:news_queries
  industry_rss  radar.yml:feeds
  hn            Hacker News via Algolia
  trends_now    Google Trends trending-now (trendspy), category-filtered
  trends_rising this repo's own data/daily/<date>.json (seo-tool rising queries)
  reddit        hot posts, only when REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set
  youtube       search.list, only when YOUTUBE_API_KEY is set

Output: a dict {generated_at, raw_items, clusters, candidates:[...]} — see run.py.
"""
from __future__ import annotations

import collections
import datetime as dt
import email.utils
import gzip
import html
import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

import requests
import yaml

log = logging.getLogger("radar.gather")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = REPO_ROOT / "radar.yml"
REDLINES_PATH = REPO_ROOT / "redlines.yml"
DAILY_DIR = REPO_ROOT / "data" / "daily"
SITEMAP_CACHE = REPO_ROOT / "data" / "radar" / "sitemap_cache.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 seo-tool-radar/1.0")
STOP = set("the a an and or of to in for on with by from is are how why what new best free vs your "
           "this that its at as be it you can will into using use just now says say".split())


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def http_get(url: str, timeout: int = 20, **kw) -> requests.Response | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, **kw)
        if r.status_code != 200:
            log.warning("GET %s -> %s", url[:90], r.status_code)
            return None
        return r
    except requests.RequestException as e:
        log.warning("GET %s failed: %s", url[:90], e)
        return None


def parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def rss_items(raw: str):
    for m in re.finditer(r"<(item|entry)[\s>](.*?)</\1>", raw, re.S):
        b = m.group(2)
        t = re.search(r"<title[^>]*>(.*?)</title>", b, re.S)
        link = re.search(r"<link[^>]*href=\"([^\"]+)\"", b) or re.search(r"<link[^>]*>(.*?)</link>", b, re.S)
        d = re.search(r"<(?:pubDate|published|updated|dc:date)[^>]*>(.*?)</", b)
        src = re.search(r"<source[^>]*>(.*?)</source>", b, re.S)
        title = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", t.group(1))).strip() if t else ""
        yield {
            "title": title,
            "url": link.group(1).strip() if link else "",
            "published": parse_date(d.group(1) if d else None),
            "source": html.unescape(src.group(1)).strip() if src else None,
        }


def toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}


class Gatherer:
    def __init__(self, cfg: dict | None = None, redlines: dict | None = None):
        self.cfg = cfg or load_yaml(CONFIG_PATH)
        self.red = redlines or load_yaml(REDLINES_PATH)
        self.now = dt.datetime.now(dt.timezone.utc)
        self.items: list[dict] = []
        self.source_stats: dict[str, int] = collections.Counter()
        self.source_errors: list[str] = []
        hb = self.red.get("hard_block", {}) or {}
        self.hard_block = [p.lower() for group in hb.values() for p in (group or [])]

    # ------------------------------------------------------------------ util
    def add(self, source_type: str, source: str, title: str, url: str = "",
            published: dt.datetime | None = None, meta: dict | None = None) -> None:
        title = (title or "").strip()
        if not title:
            return
        low = f" {title.lower()} "
        if any(p in low for p in self.hard_block):
            self.source_stats["hard_blocked"] += 1
            return
        self.items.append({
            "source_type": source_type, "source": source, "title": title, "url": url,
            "published": published.isoformat() if published else None, "meta": meta or {},
        })
        self.source_stats[source_type] += 1

    def _too_old(self, published: dt.datetime | None, max_days: float) -> bool:
        return bool(published) and (self.now - published).total_seconds() > max_days * 86400

    # --------------------------------------------------------------- sources
    def fetch_news(self) -> None:
        for q in self.cfg.get("news_queries", []):
            url = ("https://news.google.com/rss/search?q="
                   f"{urllib.parse.quote(q + ' when:2d')}&hl=en-US&gl=US&ceid=US:en")
            r = http_get(url)
            if not r:
                self.source_errors.append(f"news:{q}")
                continue
            for it in rss_items(r.text):
                self.add("news", f"gnews:{q}", it["title"], it["url"], it["published"],
                         {"publisher": it["source"]})
            time.sleep(0.3)

    def fetch_feeds(self) -> None:
        max_age = float(self.cfg.get("feed_max_age_days", 3))
        for name, url in (self.cfg.get("feeds") or {}).items():
            r = http_get(url)
            if not r:
                self.source_errors.append(f"feed:{name}")
                continue
            n = 0
            for it in rss_items(r.text):
                if self._too_old(it["published"], max_age):
                    continue
                self.add("industry_rss", name, it["title"], it["url"], it["published"])
                n += 1
            if n == 0:
                log.info("feed %s returned no fresh items", name)

    def fetch_hn(self) -> None:
        since = int((self.now - dt.timedelta(days=float(self.cfg.get("hn_max_age_days", 3)))).timestamp())
        min_pts = int(self.cfg.get("hn_min_points", 10))
        for q in self.cfg.get("hn_queries", []):
            url = ("https://hn.algolia.com/api/v1/search_by_date?query="
                   f"{urllib.parse.quote(q)}&tags=story&numericFilters=points%3E{min_pts},created_at_i%3E{since}&hitsPerPage=30")
            r = http_get(url)
            if not r:
                self.source_errors.append(f"hn:{q}")
                continue
            try:
                for h in r.json().get("hits", []):
                    self.add("hn", f"hn:{q}", h.get("title", ""),
                             h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                             parse_date(h.get("created_at")),
                             {"points": h.get("points", 0), "comments": h.get("num_comments", 0)})
            except ValueError:
                self.source_errors.append(f"hn:{q}:json")

    def fetch_trends_now(self) -> None:
        try:
            from trendspy import Trends  # already a repo dependency
            from trendspy.constants import TREND_TOPICS
        except Exception as e:  # noqa: BLE001
            self.source_errors.append(f"trends_now:import:{e}")
            return
        keep = {int(t) for t in self.cfg.get("trends_now_topics", [6, 18])}
        for geo in self.cfg.get("trends_now_geos", ["US"]):
            try:
                for x in Trends(request_delay=1.0).trending_now(geo=geo):
                    topics = [int(t) for t in (x.topics or [])]
                    cats = [TREND_TOPICS.get(str(t)) or TREND_TOPICS.get(t) for t in topics if t in keep]
                    if not cats:
                        continue
                    self.add("trends_now", f"gtrends_now:{geo}", x.keyword,
                             f"https://trends.google.com/trending?geo={geo}&q={urllib.parse.quote(x.keyword)}",
                             None, {"volume": x.volume, "categories": cats,
                                    "related": list(x.trend_keywords or [])[:5]})
            except Exception as e:  # noqa: BLE001
                self.source_errors.append(f"trends_now:{geo}:{type(e).__name__}")
            time.sleep(2)

    def fetch_rising(self) -> None:
        """This repo's own seo-tool snapshots: data/daily/<date>.json."""
        noise = self.red.get("brand_noise", {}) or {}
        for back in range(int(self.cfg.get("rising_lookback_days", 3))):
            d = (self.now.date() - dt.timedelta(days=back)).isoformat()
            p = DAILY_DIR / f"{d}.json"
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            rows = data["rows"] if isinstance(data, dict) else data
            for r in rows:
                kw, rel = r.get("Keyword", ""), r.get("Related Keyword", "")
                if any(n and n.lower() in rel.lower() for n in noise.get(kw, []) or []):
                    self.source_stats["brand_noise_dropped"] += 1
                    continue
                self.add("trends_rising", f"seo-tool:{kw}/{r.get('Region')}", rel, r.get("Source", ""),
                         parse_date(r.get("Captured At")),
                         {"trend": r.get("Trend"), "seed": kw, "region": r.get("Region"),
                          "category": r.get("Category"), "recurring": r.get("Recurring"), "date": r.get("Date")})

    def fetch_reddit(self) -> None:
        cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if not (cid and sec):
            log.info("reddit: no credentials, skipped")
            return
        try:
            tok = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, sec),
                                data={"grant_type": "client_credentials"},
                                headers={"User-Agent": UA}, timeout=20).json()["access_token"]
        except Exception as e:  # noqa: BLE001
            self.source_errors.append(f"reddit:auth:{type(e).__name__}")
            return
        hdr = {"Authorization": f"bearer {tok}", "User-Agent": UA}
        min_score = int(self.cfg.get("reddit_min_score", 50))
        for sub in self.cfg.get("subreddits", []):
            try:
                r = requests.get(f"https://oauth.reddit.com/r/{sub}/hot?limit=40", headers=hdr, timeout=20)
                for c in r.json()["data"]["children"]:
                    p = c["data"]
                    if p.get("stickied") or p.get("score", 0) < min_score:
                        continue
                    self.add("reddit", f"r/{sub}", p["title"], "https://www.reddit.com" + p["permalink"],
                             dt.datetime.fromtimestamp(p["created_utc"], dt.timezone.utc),
                             {"score": p["score"], "comments": p.get("num_comments", 0), "flair": p.get("link_flair_text")})
            except Exception as e:  # noqa: BLE001
                self.source_errors.append(f"reddit:{sub}:{type(e).__name__}")
            time.sleep(1)

    def fetch_youtube(self) -> None:
        key = os.environ.get("YOUTUBE_API_KEY")
        if not key:
            log.info("youtube: no API key, skipped")
            return
        after = (self.now - dt.timedelta(days=float(self.cfg.get("youtube_max_age_days", 3)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        for q in self.cfg.get("youtube_queries", []):
            try:
                r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=20, params={
                    "part": "snippet", "q": q, "type": "video", "order": "viewCount",
                    "publishedAfter": after, "maxResults": 15, "key": key})
                data = r.json()
                ids = [i["id"]["videoId"] for i in data.get("items", [])]
                stats = {}
                if ids:
                    s = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20,
                                     params={"part": "statistics", "id": ",".join(ids), "key": key}).json()
                    stats = {v["id"]: v.get("statistics", {}) for v in s.get("items", [])}
                for i in data.get("items", []):
                    vid = i["id"]["videoId"]
                    self.add("youtube", f"yt:{q}", i["snippet"]["title"], f"https://www.youtube.com/watch?v={vid}",
                             parse_date(i["snippet"].get("publishedAt")),
                             {"views": int(stats.get(vid, {}).get("viewCount", 0)), "channel": i["snippet"].get("channelTitle")})
            except Exception as e:  # noqa: BLE001
                self.source_errors.append(f"youtube:{q}:{type(e).__name__}")

    # ------------------------------------------------------------- sitemap
    def load_sitemap_slugs(self) -> list[str]:
        """English page paths of meshy.ai, refreshed daily and cached for offline runs."""
        cache = {}
        if SITEMAP_CACHE.exists():
            try:
                cache = json.loads(SITEMAP_CACHE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                cache = {}
        if cache.get("date") == self.now.date().isoformat() and cache.get("slugs"):
            return cache["slugs"]
        slugs: list[str] = []
        base = self.cfg.get("sitemap_index", "https://www.meshy.ai/sitemap.xml").rsplit("/", 1)[0]
        for sec in self.cfg.get("sitemap_sections", []):
            r = http_get(f"{base}/sitemap-{sec}-en.xml.gz")
            if not r:
                continue
            try:
                s = gzip.decompress(r.content).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                s = r.text
            for u in re.findall(r"<loc>(.*?)</loc>", s):
                p = u.replace("https://www.meshy.ai", "")
                if p.count("/") >= 2 or p.startswith("/blog/"):
                    slugs.append(p)
        if slugs:
            SITEMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SITEMAP_CACHE.write_text(json.dumps({"date": self.now.date().isoformat(), "slugs": slugs}), encoding="utf-8")
            return slugs
        log.warning("sitemap fetch failed; using cached %d slugs", len(cache.get("slugs", [])))
        return cache.get("slugs", [])

    # ----------------------------------------------------------- pipeline
    def run(self) -> dict:
        for step in (self.fetch_news, self.fetch_feeds, self.fetch_hn, self.fetch_trends_now,
                     self.fetch_rising, self.fetch_reddit, self.fetch_youtube):
            try:
                step()
            except Exception as e:  # noqa: BLE001
                log.exception("source %s crashed: %s", step.__name__, e)
                self.source_errors.append(f"{step.__name__}:crash")
        log.info("raw items: %d %s", len(self.items), dict(self.source_stats))

        lex = self.cfg.get("lexicon", {})
        core = [k.lower() for k in lex.get("core", [])]
        neg = [k.lower() for k in lex.get("negative", [])] + [k.lower() for k in self.red.get("off_topic", []) or []]
        w = self.cfg.get("weights", {})

        # cluster near-duplicate titles across sources
        clusters: list[dict] = []
        for it in self.items:
            tk = toks(it["title"])
            for c in clusters:
                inter = len(tk & c["toks"])
                if inter >= 3 and inter / max(1, min(len(tk), len(c["toks"]))) >= 0.5:
                    c["items"].append(it)
                    c["toks"] |= tk
                    break
            else:
                clusters.append({"toks": set(tk), "items": [it]})

        slugs = self.load_sitemap_slugs()
        generic = {"meshy", "model", "models", "free", "online", "tool", "tools", "guide", "2026", "best", "top"}

        def overlap(title: str) -> list[str]:
            tk = toks(title) - generic
            res = []
            for s in slugs:
                st = set(s.strip("/").split("/")[-1].split("-"))
                inter = tk & st
                if len(inter) >= 2:
                    res.append((len(inter), s))
            return [s for _, s in sorted(res, reverse=True)[:3]]

        cands = []
        for c in clusters:
            its = c["items"]
            title = max((i["title"] for i in its), key=len)
            low = f" {title.lower()} "
            hits = [k for k in core if k in low]
            negs = [k for k in neg if k in low]
            rel = len(hits) - 2 * len(negs)
            stypes = collections.Counter(i["source_type"] for i in its)
            nsrc = len({i["source"] for i in its})
            hn_pts = sum(i["meta"].get("points", 0) for i in its if i["source_type"] == "hn")
            rd_pts = sum(i["meta"].get("score", 0) for i in its if i["source_type"] == "reddit")
            yt_views = sum(i["meta"].get("views", 0) for i in its if i["source_type"] == "youtube")
            score = (w.get("lexicon_hit", 2) * rel
                     + w.get("extra_source", 3) * (nsrc - 1)
                     + (w.get("has_rising", 3) if "trends_rising" in stypes else 0)
                     + (w.get("has_trends_now", 2) if "trends_now" in stypes else 0)
                     + min(w.get("hn_points_cap", 4), hn_pts * w.get("hn_points_per_point", 0.02))
                     + min(w.get("reddit_score_cap", 4), rd_pts / 100)
                     + min(w.get("youtube_views_cap", 4), yt_views / 50000))
            if rel < int(self.cfg.get("min_relevance_single_source", 1)) and nsrc < 2:
                continue
            cands.append({
                "title": title, "score": round(score, 1), "relevance": rel, "keyword_hits": hits[:6],
                "negative_hits": negs, "sources": sorted({i["source"] for i in its})[:8],
                "source_types": dict(stypes), "n_items": len(its),
                "urls": [i["url"] for i in its if i["url"]][:3],
                "published": max((i["published"] or "" for i in its), default=None) or None,
                "meta": [i["meta"] for i in its if i["meta"]][:3],
                "existing_page_overlap": overlap(title),
            })
        cands.sort(key=lambda x: -x["score"])
        for i, c in enumerate(cands, 1):
            c["id"] = i
        return {
            "generated_at": self.now.isoformat(), "date": self.now.date().isoformat(),
            "raw_items": len(self.items), "clusters": len(clusters), "candidates": cands,
            "source_stats": dict(self.source_stats), "source_errors": self.source_errors,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = Gatherer().run()
    for c in out["candidates"][:40]:
        print(f"[{c['score']:>5}] {c['title'][:100]}  src={len(c['sources'])} rel={c['relevance']} overlap={c['existing_page_overlap'][:1]}")
    print(f"\nraw={out['raw_items']} clusters={out['clusters']} candidates={len(out['candidates'])} errors={out['source_errors']}")

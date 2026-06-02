"""Look up Google Trends entity IDs for each monitored brand keyword.

Google Trends "entity match" (a.k.a. topic match) consistently returns
more rising related queries than the BROAD string match pytrends uses by
default. This script queries pytrends.suggestions() for each brand in
keywords.yml that lacks an entity id, prints the top matches, and emits
a ready-to-paste YAML snippet you can drop back into keywords.yml.

Run locally (use a fresh IP — pytrends 429s aggressively):

    python scripts/discover_entities.py
    python scripts/discover_entities.py Tinkercad Blender   # ad-hoc lookups

What "type" to pick when reviewing suggestions:
  - "Online platform" / "Software" / "Topic" / "Company" → safe entity match
  - "Search term" → just BROAD, skip
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import yaml
from pytrends.exceptions import ResponseError, TooManyRequestsError
from pytrends.request import TrendReq

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "keywords.yml"

# A "Search term" entity is functionally identical to a plain BROAD
# keyword — pick a non-search-term entity when one exists.
PREFERRED_TYPES = (
    "Online platform",
    "Software",
    "Application",
    "Website",
    "Topic",
    "Company",
    "Product",
    "Brand",
)


def load_brands() -> list:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return list(cfg.get("brands", []))


def entry_to_pair(item) -> tuple[str, str | None]:
    """Return (display_name, existing_entity_or_None)."""
    if isinstance(item, str):
        return item, None
    if isinstance(item, dict):
        return item.get("name", ""), (item.get("entity") or None)
    return str(item), None


def best_suggestion(suggestions: list[dict], display_name: str) -> dict | None:
    """Pick the most-specific non-'Search term' suggestion."""
    name_lc = display_name.lower()
    # 1) Prefer matches whose title contains the search string
    relevant = [s for s in suggestions if name_lc in (s.get("title") or "").lower()]
    pool = relevant or suggestions
    # 2) Sort by preferred type rank
    def rank(s: dict) -> int:
        t = s.get("type", "")
        for i, p in enumerate(PREFERRED_TYPES):
            if p.lower() in t.lower():
                return i
        return 99
    pool.sort(key=rank)
    if not pool:
        return None
    chosen = pool[0]
    if (chosen.get("type") or "").lower() == "search term":
        return None  # no real entity available
    return chosen


def lookup(pytrends: TrendReq, term: str, retries: int = 3) -> list[dict]:
    for attempt in range(retries):
        try:
            return pytrends.suggestions(keyword=term)
        except TooManyRequestsError:
            wait = 30 * (attempt + 1) + random.uniform(0, 10)
            print(f"  429 — sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
        except ResponseError as e:
            print(f"  ResponseError: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            print(f"  Error: {e}", file=sys.stderr)
            time.sleep(5)
    return []


def main() -> int:
    pytrends = TrendReq(hl="en-US", tz=0, timeout=(10, 30))

    # If args given, lookup just those terms (ad-hoc mode).
    if len(sys.argv) > 1:
        for term in sys.argv[1:]:
            print(f"\n=== {term} ===")
            for s in lookup(pytrends, term)[:8]:
                print(f"  {s.get('mid'):20s} {s.get('type'):20s} {s.get('title')}")
            time.sleep(random.uniform(3, 6))
        return 0

    brands = load_brands()
    print(f"Looking up entity ids for {len(brands)} brands...\n", file=sys.stderr)

    yaml_snippet: list[str] = ["brands:"]
    for i, item in enumerate(brands, 1):
        name, existing = entry_to_pair(item)
        if existing:
            print(f"[{i}/{len(brands)}] {name}: already has entity {existing} — skipping",
                  file=sys.stderr)
            yaml_snippet.append(f'  - {{ name: "{name}", entity: "{existing}" }}')
            continue

        print(f"[{i}/{len(brands)}] {name}: querying...", file=sys.stderr)
        suggestions = lookup(pytrends, name)
        if not suggestions:
            print(f"  no suggestions returned", file=sys.stderr)
            yaml_snippet.append(f'  - "{name}"  # no entity found, BROAD')
        else:
            for s in suggestions[:5]:
                print(f"    {s.get('mid'):20s} {s.get('type'):20s} {s.get('title')}",
                      file=sys.stderr)
            pick = best_suggestion(suggestions, name)
            if pick:
                mid = pick.get("mid")
                pick_type = pick.get("type")
                print(f"  -> picked {mid} ({pick_type})", file=sys.stderr)
                yaml_snippet.append(
                    f'  - {{ name: "{name}", entity: "{mid}" }}  # {pick_type}'
                )
            else:
                print(f"  -> no useful entity, keeping BROAD", file=sys.stderr)
                yaml_snippet.append(f'  - "{name}"  # no entity found, BROAD')

        # Polite jitter
        time.sleep(random.uniform(3, 6))

    print("\n" + "=" * 60, file=sys.stderr)
    print("Paste below into keywords.yml under `brands:` (review first!):",
          file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("\n".join(yaml_snippet))
    return 0


if __name__ == "__main__":
    sys.exit(main())

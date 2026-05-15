# Google Trends Monitor — 3D Models

Daily monitoring dashboard for trending Google searches around **Meshy + 3D-generation competitors** and **3D-model industry/function keywords**. Built for the Meshy team to spot rising user-intent signals early.

📊 **Live dashboard:** `https://<your-org-or-user>.github.io/google-trends-monitor/`

---

## How it works

Every day at **09:30 Asia/Shanghai (01:30 UTC)**, [`.github/workflows/daily.yml`](.github/workflows/daily.yml) runs [`src/monitor.py`](src/monitor.py), which:

1. Loads keywords from [`keywords.yml`](keywords.yml).
2. For each `keyword × region` pair, queries Google Trends via [pytrends](https://github.com/GeneralMills/pytrends) for **Rising related queries** in the last 7 days.
3. Keeps only rising queries whose % change is **≥ 500%** (Google's "Rising" threshold) plus all **Breakout** queries.
4. Writes:
   - `docs/latest.json` — current snapshot read by the dashboard
   - `data/daily/YYYY-MM-DD.json` — per-day archive
   - `data/history.csv` — append-only full history
5. Commits the changes back to `main` so the dashboard auto-updates.

## Dashboard columns

| Column | Meaning |
|---|---|
| **Keyword** | Brand or industry keyword from `keywords.yml` |
| **Region** | Global / United States / Germany / Japan / France |
| **Date** | UTC date of the run |
| **Related Keyword** | The rising query Google surfaced for that keyword + region |
| **Trend** | Either `+NNN%` (Rising ≥500%) or `Breakout` (>5000%) |
| **Type** | `Rising` (always — Top queries are excluded to focus on movers) |
| **Verify** | Click-through to Google Trends to double-check the data |

The dashboard also supports keyword/region/type filtering, free-text search, sortable columns, and CSV export.

## Configuration

Everything user-facing lives in [`keywords.yml`](keywords.yml):

- **`brands`** — competitor brand list (Meshy, Tripo, Sketchfab, Luma AI, Rodin AI, CSM AI, Spline, Kaedim, Alpha3D, Polycam, Scenario, 3DFY AI, Masterpiece Studio).
- **`industry`** — generic 3D-model intent keywords (`3d model`, `text to 3d`, `image to 3d`, `3d printing`, …).
- **`regions`** — list of `{ code, name }`. Empty code = Global.
- **`threshold`** — minimum % change for a rising query to be kept (default `500`).
- **`timeframe`** — pytrends timeframe token (default `now 7-d`).

Edit, commit, push — the next scheduled run picks it up.

## Setup (one-time)

```bash
# 1. Push this folder to GitHub
gh repo create google-trends-monitor --public --source=. --push
# (or create manually and `git remote add origin … && git push -u origin main`)

# 2. Enable GitHub Pages
#    Settings → Pages → Source: "Deploy from a branch"
#                       Branch: main  /  Folder: /docs

# 3. Allow Actions to commit back
#    Settings → Actions → General → Workflow permissions:
#       Select "Read and write permissions"

# 4. (Optional) Trigger the first run manually
#    Actions → "Daily Google Trends Monitor" → Run workflow
```

After the first successful run, the dashboard at `https://<owner>.github.io/google-trends-monitor/` will have data.

## Local development

```bash
pip install -r requirements.txt
python src/monitor.py
open docs/index.html        # or: python -m http.server -d docs 8000
```

A local run takes ~3–5 minutes (rate-limit-friendly sleeps between requests).

## Caveats

- **pytrends is unofficial.** Google occasionally rate-limits (HTTP 429); the script retries with backoff and skips keywords that exhaust retries. If failures become frequent, switch to a paid API (SerpAPI's `google_trends` endpoint is the easiest drop-in).
- **No Rising data ≠ nothing trending.** Google only surfaces rising queries when there's enough search volume. Quiet days will produce empty snapshots — that's normal.
- All timestamps in data files are **UTC**; the dashboard renders in the viewer's local timezone.

## Files

```
google-trends-monitor/
├── keywords.yml                  # ← edit this
├── requirements.txt
├── src/monitor.py                # collector
├── docs/
│   ├── index.html                # dashboard (served by GitHub Pages)
│   └── latest.json               # latest snapshot
├── data/
│   ├── history.csv               # append-only history
│   └── daily/YYYY-MM-DD.json     # daily snapshots
└── .github/workflows/daily.yml   # cron 01:30 UTC daily
```

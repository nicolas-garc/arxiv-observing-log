# arXiv Observing Log

An automated **daily arXiv paper finder for ML × astrophysics** (galaxies,
cosmology, simulations). A nightly job cuts a fresh "edition" of the most
relevant new submissions and publishes it to GitHub Pages, where a static front
end renders it instantly.

- **Live site:** `https://nicolas-garc.github.io/arxiv-observing-log/`


## How it works

```
config/keywords.json   ── your interest profile (seed keywords)
scripts/build_edition.py ── nightly builder (Python 3, requests only)
        │  queries the arXiv API for the last 48h of
        │  astro-ph.GA / astro-ph.CO / astro-ph.IM submissions,
        │  scores + glosses them, then writes ↓
data/latest.json       ── the current edition the site loads
data/editions/YYYY-MM-DD.json ── dated archive of every edition
index.html             ── the static front end (loads latest.json,
                          falls back to a live arXiv fetch if it's stale)
.github/workflows/nightly.yml ── the 3 AM scheduler
```

The front end loads `data/latest.json`. If that file is missing or older than
36 hours it falls back to fetching the arXiv API live in the browser, so the
page always shows *something* even if a build is skipped.

### Scoring

Each paper is scored against `config/keywords.json`:

- **Title hits × 4**
- **Abstract hits × 1**, capped at 3 per keyword
- **+3** if the paper is cross-listed to an ML category
  (`cs.LG`, `stat.ML`, `cs.CV`, `cs.AI`)

Papers scoring **5+** are shown as full cards with an extractive one-line gloss;
**1–4** show as compact rows; the rest are title-only. (The front end recomputes
this score in the browser too, so live edits to your keyword list re-rank the
feed instantly.)

## Editing your keywords

Edit **`config/keywords.json`** and commit. The next nightly build picks it up
automatically.

```json
{
  "keywords": [
    "machine learning",
    "deep learning",
    "graph neural network",
    "your new keyword here"
  ]
}
```

You can also add/remove keywords ad hoc in the browser (they're stored in
`localStorage` and only affect your own view); `config/keywords.json` is the
shared default used to build the published edition.

## The 3 AM cron — and how to change it

The schedule lives in [`.github/workflows/nightly.yml`](.github/workflows/nightly.yml):

```yaml
on:
  schedule:
    - cron: "0 8 * * *"   # 08:00 UTC = 03:00 America/New_York (EDT)
  workflow_dispatch: {}    # lets you run it manually from the Actions tab
```

**GitHub Actions cron is always in UTC.** `0 8 * * *` means 08:00 UTC, which is
03:00 in US Eastern Daylight Time. A few notes:

- **To change the time,** edit the cron expression. The five fields are
  `minute hour day-of-month month day-of-week`. For example, 06:00 UTC daily is
  `0 6 * * *`.
- **To target a different timezone,** convert your desired local time to UTC and
  use that. For example, 03:00 in US Pacific (PDT, UTC−7) is `0 10 * * *`; 03:00
  in Central European Summer Time (UTC+2) is `0 1 * * *`. There is no timezone
  field — always express the hour in UTC.
- **Daylight saving:** because the cron is fixed in UTC, the *local* trigger time
  shifts by an hour when your region switches DST. If you need it pinned to
  exactly 3 AM local year-round, adjust the UTC hour when the clocks change.
- Actions cron can **drift by a few minutes** (and occasionally more under load);
  that's expected and harmless for a nightly edition.
- You can always trigger a build by hand: **Actions → Nightly edition build →
  Run workflow** (this is the `workflow_dispatch` trigger).

## Running the build locally

Requires Python 3 and `requests`:

```bash
pip install requests
python scripts/build_edition.py
```

This writes `data/latest.json` and `data/editions/<today>.json`. To preview the
site, serve the repo root over HTTP (the front end fetches `data/latest.json`, so
opening `index.html` via `file://` will hit browser CORS restrictions):

```bash
python -m http.server 8000
# then open http://localhost:8000/
```

### Widening the window

By default the builder looks back **48 hours** (matching the nightly cadence).
arXiv only announces new listings Sun–Thu evenings ET, so after a weekend or
holiday the last 48h can be empty. To cut a wider one-off edition, set
`ARXIV_WINDOW_HOURS`:

```bash
ARXIV_WINDOW_HOURS=168 python scripts/build_edition.py   # last week
```

The nightly workflow does **not** set this, so scheduled builds always use 48h.

## Failure behavior

The builder is designed to **never crash the build**. If the arXiv API is
unreachable or returns something unparseable, it logs a warning and exits
cleanly, leaving the previous `data/latest.json` in place so the site keeps
serving the last good edition.

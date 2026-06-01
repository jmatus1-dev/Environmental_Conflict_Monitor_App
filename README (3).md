# Environmental Conflict Monitor

Scrapes Latin American environmental-conflict news from three sources, merges
it into one dataset, geocodes it, and serves it as an interactive dashboard.

Everything in the default pipeline is **free** and runs on your own machine.
There is one *optional, paid* AI step that is turned off by default — see
"Optional paid step" below and `LLM_COST_MEMO.md`.

    scrape              geocode             dashboard
    run_all.py   -->    geocode.py    -->   app.py
        |                   |                  |
        +------ data/combined.csv (one growing dataset) ------+

## Files

- `common.py` — shared foundation: the `Article` model, the CSV schema, the
  robots-aware `PoliteSession`, text helpers, and the CSV merge/dedupe
  utilities (all writes are atomic).
- `scraper_elespectador.py`, `scraper_infoamazonia.py`, `scraper_mongabay.py`
  — one per site; only site-specific logic.
- `run_all.py` — Step 1. Runs all scrapers, merges into a growing combined CSV.
- `geocode.py` — Step 3. Turns locations into latitude/longitude (free
  OpenStreetMap; no API key).
- `app.py` — Step 4. The Streamlit dashboard.
- `enrich_llm.py` — Step 2, OPTIONAL + PAID. Off by default. See below.
- `LLM_COST_MEMO.md` — a plain-language explainer of the optional paid step.

## Setup (free pipeline)

    pip install -r requirements.txt   # installs only the free/core packages

## Run (free pipeline)

    python run_all.py        # 1. scrape  -> data/combined.csv
    python geocode.py        # 3. add latitude/longitude
    streamlit run app.py     # 4. open the dashboard

That's the whole thing. No accounts, no keys, no charges.

## What each stage writes to combined.csv

- **Step 1** writes the 18 base columns (title, url, date, country, sector,
  event_type, ...). Some are blank when the site's HTML didn't expose them.
- **Step 3** adds `latitude`, `longitude`, `geocode_precision`,
  `geocode_status`. Without the optional AI step, it geocodes to the
  **region or country level** (the most specific place the scraper can read).

Re-running the scraper keeps existing rows over fresh re-scrapes, so geocoded
coordinates (and any other added columns) survive future runs.

## Optional paid step (Step 2 — AI enrichment)

`enrich_llm.py` reads each article's full text with Claude and fills in fields
the HTML can't (severity, urgency, a neutral summary, the responsible actor,
and the *specific* place — which upgrades the map from region-level to exact
pins). **It calls the Anthropic API, a metered pay-per-use service, and is off
by default.**

Read `LLM_COST_MEMO.md` first (it's written to share with a supervisor). Rough
cost with the cheapest model: well under a cent per article, ~$3–5 per 1,000.

To enable it, once approved:

    # 1. install the optional packages (uncomment them in requirements.txt first)
    pip install anthropic trafilatura
    # 2. set up a key at https://platform.claude.com and export it
    export ANTHROPIC_API_KEY=sk-ant-...
    # 3. run a small paid test FIRST to check quality + see the charge
    python enrich_llm.py --limit 20
    # then geocode again to use the new precise locations
    python geocode.py

## Notes

- **Geocoder:** `geopy` + OpenStreetMap/Nominatim (free, ~1 req/sec), results
  cached in `data/geocode_cache.json`. A paid backend (Google/Mapbox/OpenCage)
  can be swapped into the `Geocoder` class for higher accuracy.
- **Cross-source duplicates** (same event in two outlets) are not merged by
  rules — that's a natural future job for the optional AI step.

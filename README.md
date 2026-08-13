# Environmental Conflict Monitor

Scrapes news about environmental conflicts (mining, oil, deforestation,
water, indigenous land) from **8 Latin-American and international outlets**,
geocodes each story, and serves everything as an interactive **Streamlit
dashboard** — a map of conflicts with filters, time trends, and article
details. An **Admin Mode** adds a satellite analysis pipeline: for any
conflict it downloads Landsat imagery, tracks 8 environmental indicators
over time, renders before/after maps, and generates a **PDF slide deck**
(English or Spanish) branded for AImpact Lab.

Built by Julieta Matus during a summer internship with AImpact Lab,
supervised by Hernando Grueso and Will Nielsen.

---

## Quick start — run the dashboard

```bash
git clone https://github.com/jmatus1-dev/Environmental_Conflict_Monitor_App.git
cd Environmental_Conflict_Monitor_App
pip install -r requirements.txt
streamlit run app.py
```

That's it — the app opens at `http://localhost:8501` with the dataset that
ships in the repo (`data/combined.csv`). No accounts, no API keys.

> On some Macs, if `pip install` complains about "externally managed
> environment", use `pip install -r requirements.txt --break-system-packages`.

---

## Updating the data (scrape → geocode)

The dataset is a *growing* database: re-running the pipeline adds new
articles and never loses old ones (rows that scroll off a site's front page
are kept; already-enriched rows survive re-scrapes).

```bash
python3 download_shapefiles.py   # one-time: GADM boundary files (for admin matching)
python3 run_all.py               # scrape all 8 sources -> data/combined.csv
python3 geocode.py               # add latitude/longitude (free OpenStreetMap)
streamlit run app.py             # see the refreshed map
```

What `run_all.py` does per run: scrapes each source (capped per source so no
outlet floods the set), merges + de-duplicates into `data/combined.csv`,
assigns stable conflict IDs, fetches full article text, and matches each
article to its municipality (GADM admin boundaries). Useful flags:
`--only mongabay` (one source), `--max-per-source 100`, `--fresh` (rebuild).

**Sources:** El Espectador, Mongabay Latam, InfoAmazonia, The Guardian,
Dialogue Earth, Ojo Público, Agência Pública, Grist.

---

## Admin Mode — satellite analysis + slide decks

In the app's sidebar, switch to **Admin Mode → Setup & Run** to run a
satellite analysis for a conflict: pick the conflict, time window, industry,
and report language, then **Run Analysis**.

For each of 8 indicators — NDVI, EVI2, SAVI, NDMI (vegetation) · NDWI,
MNDWI (water) · NDBI, BSI (soil/built-up) — the app downloads monthly
Landsat composites (Microsoft Planetary Computer, free, no account),
plots the indicator's time series inside the conflict's boundary, and
renders **before/after maps** (first vs. last month of the window, shared
color scale). A **Generate slide deck** button then produces a branded PDF
(title slide → contents → one slide per plot → closing), in English or
Spanish, following AImpact Lab's deck format.

This part runs **locally only** (not on the deployed Streamlit Cloud site)
and needs two extra one-time setups:

```bash
# 1. Geospatial + satellite packages
pip install pystac-client planetary-computer stackstac rioxarray \
            geopandas matplotlib --break-system-packages

# 2. Quarto (renders the slide decks): download the macOS/Windows/Linux
#    installer from https://quarto.org/docs/get-started/ and run it.
#    PDF conversion uses Google Chrome, which you almost certainly have.
```

Outputs land in `outputs/analyses/<conflict>/`: monthly GeoTIFF rasters,
a per-month CSV of indicator means, all plots as PNGs, and the PDF deck.

---

## Project structure

| File | Role |
|---|---|
| `app.py` | The Streamlit dashboard (View mode + Admin Mode) |
| `run_all.py` | Pipeline entry point: scrape all sources, merge, enrich |
| `common.py` | Shared foundation: `Article` model, CSV schema, polite HTTP session, merge/dedupe |
| `scraper_*.py` (×8) | One per news outlet; site-specific logic only |
| `conflict_ids.py` | Groups articles into stable conflict IDs |
| `fetch_text.py` | Downloads full article text |
| `geocode.py` | Locations → coordinates (Nominatim/OpenStreetMap, cached) |
| `match_admin.py` | Matches articles to municipalities (GADM boundaries) |
| `download_shapefiles.py` | One-time download of GADM 4.1 boundary files |
| `satellite_pilot.py` | Satellite indicator analysis (Landsat via Planetary Computer) |
| `report_slides.py` | Quarto slide deck generator (PDF, en/es) |
| `slides_theme.scss` | AImpact Lab slide theme |
| `data/combined.csv` | The dataset — one row per article |

## Data sources & credits

- News content belongs to the respective outlets; the scrapers respect
  robots.txt and rate-limit themselves.
- Administrative boundaries: [GADM 4.1](https://gadm.org) (free for
  academic / non-commercial use).
- Satellite imagery: Landsat Collection 2 via
  [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com).
- Geocoding: [Nominatim / OpenStreetMap](https://nominatim.org).

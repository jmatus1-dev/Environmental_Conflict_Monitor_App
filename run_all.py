"""
run_all.py - the single entry point for the whole pipeline.

It runs each site scraper, writes a per-source CSV for debugging, then merges
everything into ONE combined dataset. The combined file is a *growing*
database: each run loads what's already there, adds the new articles, drops
duplicates, and writes the union back. That means:

    * articles that scroll off a site's front page are NOT lost on later runs;
    * you never re-process an article you've already seen (important once the
      LLM-extraction and geocoding steps start costing time/money per row);
    * if an existing row already has extra columns filled in (e.g. latitude,
      severity), those survive — the already-saved row wins over a fresh,
      bare re-scrape of the same URL.

Typical use:
    python run_all.py                          # grow data/combined.csv
    python run_all.py --max-per-source 100     # pull more per site
    python run_all.py --fresh                  # ignore existing, rebuild
    python run_all.py --only mongabay          # run a single source

The combined CSV is what steps 2-4 (LLM extraction, geocoding, the Streamlit
app) will read.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

import conflict_ids
import fetch_text
import match_admin
import common
import scraper_elespectador as ee
import scraper_infoamazonia as ia
import scraper_mongabay as mb

# The registry of available scrapers. Adding a 4th source later = one line.
SCRAPERS = {
    "elespectador": ee,
    "infoamazonia": ia,
    "mongabay": mb,
}

DEFAULT_OUT = os.path.join("data", "combined.csv")
DEFAULT_PER_SOURCE_DIR = os.path.join("data", "per_source")


def run_one(key: str, module, max_articles: int, delay: float,
            per_source_dir: str) -> list:
    """Run a single scraper, write its per-source CSV, return its Articles.
    A crash in one source is logged and swallowed so the others still run."""
    logging.info("=== Running scraper: %s (%s) ===", key, module.SOURCE_NAME)
    try:
        session = module.make_session(delay=delay)
        articles = module.crawl(max_articles, session)
    except Exception as e:  # noqa: BLE001 - we deliberately keep going
        logging.exception("Scraper '%s' failed and was skipped: %s", key, e)
        return []

    per_source_path = os.path.join(per_source_dir, f"{key}.csv")
    common.write_csv(articles, per_source_path)
    logging.info("  %s: %d articles -> %s", key, len(articles), per_source_path)
    return articles


def run_all(selected_keys: list[str], max_per_source: int, out_path: str,
            per_source_dir: str, delay: float, fresh: bool) -> None:
    started = datetime.now()

    # 1. Run each selected scraper and gather fresh Articles.
    fresh_articles = []
    for key in selected_keys:
        fresh_articles.extend(
            run_one(key, SCRAPERS[key], max_per_source, delay, per_source_dir))

    fresh_rows = common.articles_to_rows(fresh_articles)
    logging.info("Scraped %d fresh articles across %d source(s).",
                 len(fresh_rows), len(selected_keys))

    # 2. Merge with the existing combined file (unless --fresh).
    #    Existing rows go FIRST so that, on a duplicate, the already-saved row
    #    (which may carry LLM/geocode columns) wins over the bare re-scrape.
    if fresh:
        existing_rows = []
        logging.info("--fresh: ignoring any existing %s.", out_path)
    else:
        existing_rows = common.read_rows(out_path)
        logging.info("Loaded %d existing rows from %s.",
                     len(existing_rows), out_path)

    merged = existing_rows + fresh_rows
    before = len(merged)
    deduped = common.dedupe_rows(merged)
    deduped = common.sort_rows_by_date(deduped)
    logging.info("Merged %d rows -> %d after de-duplication (%d dropped).",
                 before, len(deduped), before - len(deduped))

    # 3. Write the combined dataset.
    common.write_rows(deduped, out_path)
    elapsed = (datetime.now() - started).total_seconds()
    new_added = len(deduped) - len(existing_rows)
    logging.info("Wrote %d rows to %s in %.1fs (%d new this run).",
                 len(deduped), out_path, elapsed, max(new_added, 0))

    # 4. Assign stable conflict IDs across the combined dataset.
    try:
        conflict_ids.main()
    except Exception as e:  # noqa: BLE001 - don't lose the scraped data if this fails
        logging.exception("Conflict ID step failed and was skipped: %s", e)

    # 5. Fetch the full article text for any new rows that don't have it yet.
    try:
        fetch_text.main([])
    except Exception as e:  # noqa: BLE001 - don't lose the scraped data if this fails
        logging.exception("Full-text fetch step failed and was skipped: %s", e)

    # 6. Match each article down to its municipality (admin_2 / admin_3) and
    #    record the gadm_id, using the article_text just fetched.
    try:
        match_admin.main([])
    except Exception as e:  # noqa: BLE001 - don't lose the scraped data if this fails
        logging.exception("Admin match step failed and was skipped: %s", e)


def parse_cli(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--max-per-source", type=int, default=50,
                   help="Max relevant articles to keep per source (default 50). "
                        "Acts as an equal cap so no single source floods the set.")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help=f"Combined CSV path (default: {DEFAULT_OUT})")
    p.add_argument("--per-source-dir", default=DEFAULT_PER_SOURCE_DIR,
                   help="Where to write the per-source debug CSVs.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between HTTP requests (default 1.0).")
    p.add_argument("--fresh", action="store_true",
                   help="Rebuild from scratch instead of growing the existing file.")
    p.add_argument("--only", choices=sorted(SCRAPERS), default=None,
                   help="Run only one source (default: all).")
    p.add_argument("--verbose", action="store_true",
                   help="Debug-level logging.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_cli(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    selected = [args.only] if args.only else list(SCRAPERS)
    run_all(selected, args.max_per_source, args.out, args.per_source_dir,
            args.delay, args.fresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

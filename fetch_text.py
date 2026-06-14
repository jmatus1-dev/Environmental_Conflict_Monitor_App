"""
fetch_text.py - capture the full body text of each article.

Adds two columns to data/combined.csv:

  * article_text  - the main body text of the article, pulled from its page
  * text_status   - 'ok', 'empty', or 'error:<reason>', so messy / failed rows
                    are easy to filter later WITHOUT having to re-fetch them

Why this is its own script
--------------------------
It mirrors geocode.py: one stage that reads the single combined CSV, fills a
blank column for the rows that don't have it yet, and writes the file back. That
means:
  * it BACK-FILLS the articles you already have (editing the scrapers would
    only affect newly-scraped rows, because run_all keeps the existing row over
    a bare re-scrape);
  * it's incremental + idempotent - a row that already has article_text is
    skipped, so you never re-fetch the same page;
  * the text survives future scrapes, because run_all's "existing row wins"
    de-dupe keeps the (now text-filled) row.

It is FREE: it only downloads each page and extracts the main text locally with
trafilatura. No API key, no LLM, no charges. (The paid enrich_llm.py is a
separate, optional step and is untouched by this.)

Run
---
    pip install trafilatura            # one-time, if you don't have it yet
    python fetch_text.py               # fill article_text for the whole file
    python fetch_text.py --limit 20    # do only 20 rows first (a safe trial)
    python fetch_text.py --refetch     # retry rows whose last fetch errored

    # point it at a different file if needed:
    ENVCONFLICT_DATA=data/combined.csv python fetch_text.py
"""

from __future__ import annotations

import argparse
import logging
import os
from urllib.parse import urlparse

import common  # reuses your PoliteSession + read_rows/write_rows

TEXT_COL = "article_text"
STATUS_COL = "text_status"
DATA_PATH = os.environ.get("ENVCONFLICT_DATA", os.path.join("data", "combined.csv"))


class BodyFetcher:
    """Fetch an article page and extract its main text. Keeps one polite,
    robots-aware session per domain (the same PoliteSession the scrapers use),
    so we honour robots.txt and rate limits per site."""

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self._sessions: dict[str, object] = {}

    def _session_for(self, url: str):
        netloc = urlparse(url).netloc
        if netloc not in self._sessions:
            base = f"{urlparse(url).scheme}://{netloc}"
            self._sessions[netloc] = common.PoliteSession(base, delay=self.delay)
        return self._sessions[netloc]

    def text_for(self, url: str) -> tuple[str, str]:
        """Return (text, status). status is 'ok', 'empty', or 'error:<reason>'.
        Never raises on a bad page - a failure just becomes a status string, so
        one broken URL can't stop the whole run (Hernando's 'keep it even if
        messy')."""
        if not url:
            return ("", "error:no_url")
        try:
            import trafilatura
        except ImportError:
            raise SystemExit("trafilatura is required: pip install trafilatura")

        try:
            r = self._session_for(url).get(url)
        except Exception as e:  # noqa: BLE001
            return ("", f"error:fetch_{type(e).__name__}")
        if r is None:
            return ("", "error:fetch_failed")

        try:
            text = trafilatura.extract(
                r.text, include_comments=False, include_tables=False
            ) or ""
        except Exception as e:  # noqa: BLE001
            return ("", f"error:extract_{type(e).__name__}")

        text = text.strip()
        return (text, "ok") if text else ("", "empty")


def _needs_text(row: dict, refetch: bool) -> bool:
    """True if this row should be (re)fetched on this run."""
    if row.get(TEXT_COL):                       # already has text -> skip
        return False
    if refetch:                                 # --refetch: retry everything blank
        return True
    # default: do fresh rows; leave previously-errored rows alone unless --refetch
    return not str(row.get(STATUS_COL, "")).startswith("error")


def fetch_texts(path: str, limit: int | None = None,
                refetch: bool = False, delay: float = 1.0) -> None:
    rows = common.read_rows(path)
    fetcher = BodyFetcher(delay=delay)

    todo = [r for r in rows if _needs_text(r, refetch)]
    if limit is not None:
        todo = todo[:limit]

    logging.info("%d of %d rows need body text.", len(todo), len(rows))
    for i, row in enumerate(todo, 1):
        url = row.get("article_url", "")
        text, status = fetcher.text_for(url)
        row[TEXT_COL] = text
        row[STATUS_COL] = status
        logging.info("[%d/%d] %-7s %4d chars  %s",
                     i, len(todo), status, len(text), url[:70])

    # Ensure every row carries both columns so the CSV header stays consistent.
    for row in rows:
        row.setdefault(TEXT_COL, "")
        row.setdefault(STATUS_COL, "")

    common.write_rows(rows, path)
    ok = sum(1 for r in rows if r.get(STATUS_COL) == "ok")
    empty = sum(1 for r in rows if r.get(STATUS_COL) == "empty")
    err = sum(1 for r in rows if str(r.get(STATUS_COL, "")).startswith("error"))
    logging.info("Done. %d ok, %d empty, %d errored. Wrote %s.",
                 ok, empty, err, path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Add a full-text (article_text) column to combined.csv.")
    p.add_argument("--in", dest="in_path", default=DATA_PATH,
                   help=f"Combined CSV to fill in place (default: {DATA_PATH}).")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N rows (good for a first trial run).")
    p.add_argument("--refetch", action="store_true",
                   help="Retry rows whose previous fetch errored.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between fetches to the same site (default 1.0).")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(args.in_path):
        raise SystemExit(f"No data file at '{args.in_path}'. Run run_all.py first.")

    fetch_texts(args.in_path, args.limit, args.refetch, args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

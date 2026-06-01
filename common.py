"""
common.py - shared foundation for the AImpact Lab environmental-conflict scrapers.

Everything that the three site scrapers used to each define on their own now
lives here, in ONE place:

    * the Article data model and the CSV column order (CSV_FIELDS)
    * PoliteSession  - the robots.txt-aware, rate-limited HTTP client
    * the small text helpers (meta-tag reading, date parsing, keyword matching,
      accent-insensitive region detection, stable article IDs)
    * the CSV read/write + merge/deduplicate utilities the orchestrator uses

Each scraper_<site>.py imports from this module and only keeps the parts that
are genuinely unique to its website (which pages to crawl, how that site labels
its articles, and how to read one of its article pages).

Nothing here talks to a specific website, so this module never needs to change
when you tweak one scraper.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import random
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from dateutil import parser as dateparser

# A single descriptive User-Agent for every request from every scraper.
USER_AGENT = (
    "AImpactLabBot/0.1 (+research; environmental conflict monitoring; "
    "contact: intern@aimpactlab.example)"
)


# ---------------------------------------------------------------------------
# Data model  (the single source of truth for the dataset schema)
# ---------------------------------------------------------------------------

@dataclass
class Article:
    """One row of the dataset.

    The first 18 fields ARE the CSV schema, in order. `extra` is a catch-all
    for source-specific debug info (e.g. Mongabay's topic slugs) that we want
    to keep in memory for the relevance filter but never write to the CSV.
    """
    article_id: str
    source: str
    article_title: str
    article_url: str
    date_published: str
    country: str
    region_department: str
    locality: str
    latitude: str
    longitude: str
    sector: str
    actor_company: str
    community_actor: str
    event_type: str
    environmental_issue: str
    source_text_excerpt: str
    extraction_method: str
    notes: str

    # Internal-only; never written to the CSV. Holds things like
    # {"topic_slugs": [...], "region_slugs": [...], "raw_keywords": [...]}.
    extra: dict = field(default_factory=dict, repr=False)


# The CSV column order. Defined once so all three scrapers and the combined
# file are guaranteed to agree.
CSV_FIELDS = [
    "article_id", "source", "article_title", "article_url", "date_published",
    "country", "region_department", "locality", "latitude", "longitude",
    "sector", "actor_company", "community_actor", "event_type",
    "environmental_issue", "source_text_excerpt", "extraction_method", "notes",
]


# ---------------------------------------------------------------------------
# HTTP client  (polite: respects robots.txt + waits between requests)
# ---------------------------------------------------------------------------

class PoliteSession:
    """A thin wrapper around requests.Session that (a) waits a short, jittered
    delay between requests so we don't hammer a site, and (b) checks the site's
    robots.txt and refuses to fetch disallowed URLs.

    `base_url` is required because robots.txt lives at the site root, and the
    three sites have different roots.
    """

    def __init__(self, base_url: str, delay: float = 1.0, jitter: float = 0.5,
                 timeout: int = 20, accept_language: str = "es-419,es;q=0.9,en;q=0.7"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": accept_language,
        })
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self._last_request_ts: float = 0.0
        self._robots = self._load_robots()

    def _load_robots(self) -> Optional[RobotFileParser]:
        rp = RobotFileParser()
        rp.set_url(urljoin(self.base_url, "/robots.txt"))
        try:
            rp.read()
            return rp
        except Exception as e:  # network / parsing problems shouldn't be fatal
            logging.warning("Could not load robots.txt for %s (%s); proceeding "
                            "without robots checks.", self.base_url, e)
            return None

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        return self._robots.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> Optional[requests.Response]:
        if not self.allowed(url):
            logging.info("Skipping %s (disallowed by robots.txt)", url)
            return None

        # Enforce the minimum delay since the previous request.
        elapsed = time.time() - self._last_request_ts
        wait = self.delay - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, self.jitter))

        try:
            r = self.session.get(url, timeout=self.timeout)
            self._last_request_ts = time.time()
        except requests.RequestException as e:
            logging.warning("Request failed for %s: %s", url, e)
            return None

        if r.status_code != 200:
            logging.info("Got %s for %s", r.status_code, url)
            return None
        return r


# ---------------------------------------------------------------------------
# Small shared text helpers
# ---------------------------------------------------------------------------
# These take a BeautifulSoup object but we don't import bs4 here; we only call
# methods on whatever object is passed in. That keeps common.py dependency-light.

def meta(soup, name: str) -> Optional[str]:
    """Read the content of <meta name="..."> OR <meta property="...">."""
    tag = soup.find("meta", attrs={"name": name})
    if tag is None:
        tag = soup.find("meta", attrs={"property": name})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def meta_any(soup, names: Iterable[str]) -> Optional[str]:
    """Return the first non-empty meta value among `names`."""
    for n in names:
        v = meta(soup, n)
        if v:
            return v
    return None


def normalise_date(raw: Optional[str]) -> str:
    """Turn any parseable date string into YYYY-MM-DD. If it can't be parsed,
    return it unchanged so a human can review it; if empty, return ""."""
    if not raw:
        return ""
    try:
        return dateparser.parse(raw).date().isoformat()
    except (ValueError, TypeError):
        return raw


def strip_accents(s: str) -> str:
    """Remove diacritics so 'Caquetá' and 'Caqueta' compare equal."""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def word_match(blob: str, kw: str) -> bool:
    """True if `kw` appears in `blob` with a word boundary on each side.

    Word boundaries stop short keywords from matching inside longer words
    (e.g. 'para' inside the Spanish preposition). Both inputs are expected to
    be lowercased already. \\b is Unicode-aware in Python 3 so accented
    characters count as word characters.
    """
    return bool(re.search(rf"\b{re.escape(kw)}\b", blob, flags=re.UNICODE))


def classify_by_keyword(blob: str, mapping: dict[str, list[str]]) -> str:
    """Return the FIRST label whose keyword list has a hit in `blob`, else ''.

    `mapping` is {label: [keyword, ...]}. Insertion order encodes priority.
    """
    for label, kws in mapping.items():
        for kw in kws:
            if word_match(blob, kw.lower()):
                return label
    return ""


def detect_country(blob: str, country_keywords: dict[str, list[str]],
                   default: str = "") -> str:
    """First country whose keyword list matches `blob`, else `default`."""
    for country, kws in country_keywords.items():
        for kw in kws:
            if word_match(blob, kw.lower()):
                return country
    return default


def detect_in_list(text: str, names: list[str]) -> str:
    """First name in `names` that appears in `text`, accent-insensitively.
    Returns the canonical (original, accented) form from `names`. Used for
    region/department detection."""
    text_norm = strip_accents(text).lower()
    for name in names:
        name_norm = strip_accents(name).lower()
        if re.search(rf"\b{re.escape(name_norm)}\b", text_norm):
            return name
    return ""


def make_url_id(prefix: str, url: str) -> str:
    """Build a stable, source-prefixed article id from a URL.
    The same URL always produces the same id, which is what lets the
    orchestrator deduplicate across runs."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:url-{digest}"


# ---------------------------------------------------------------------------
# CSV read / write + merge / deduplicate
# ---------------------------------------------------------------------------

def article_to_row(a: Article) -> dict:
    """Project an Article down to just the CSV columns (drops `extra`)."""
    return {k: getattr(a, k) for k in CSV_FIELDS}


def articles_to_rows(articles: list[Article]) -> list[dict]:
    return [article_to_row(a) for a in articles]


def write_csv(articles: list[Article], path: str) -> None:
    """Write a list of Article objects to `path` using exactly CSV_FIELDS.
    Used for the per-source debug files."""
    _ensure_parent(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for a in articles:
            w.writerow(article_to_row(a))
    os.replace(tmp, path)  # atomic swap: a crash mid-write can't corrupt path


def read_rows(path: str) -> list[dict]:
    """Read an existing combined CSV into a list of dict rows. Returns []
    if the file doesn't exist. Preserves ALL columns found in the file, even
    ones added later by the LLM / geocoding steps, so enrichment is never
    silently dropped on the next run."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """Keep the FIRST occurrence of each article, judged by article_id first
    and article_url second. Because the orchestrator puts already-saved rows
    before freshly scraped ones, an article that already exists (possibly with
    LLM/geocode columns filled in) wins over a fresh, bare re-scrape."""
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    out: list[dict] = []
    for r in rows:
        aid = (r.get("article_id") or "").strip()
        url = (r.get("article_url") or "").strip()
        if aid and aid in seen_ids:
            continue
        if url and url in seen_urls:
            continue
        if aid:
            seen_ids.add(aid)
        if url:
            seen_urls.add(url)
        out.append(r)
    return out


def write_rows(rows: list[dict], path: str) -> None:
    """Write dict rows to `path`. The header is CSV_FIELDS followed by any
    extra columns present in the data (in first-seen order), so later
    enrichment columns survive. Missing values are written as ""."""
    _ensure_parent(path)
    fieldnames = list(CSV_FIELDS)
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    os.replace(tmp, path)  # atomic swap


def sort_rows_by_date(rows: list[dict]) -> list[dict]:
    """Newest first. Empty/unknown dates sink to the bottom."""
    return sorted(rows, key=lambda r: r.get("date_published") or "0000-00-00",
                  reverse=True)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


# Re-export asdict so scrapers that want a raw JSONL dump don't need to import
# dataclasses themselves.
__all__ = [
    "USER_AGENT", "Article", "CSV_FIELDS", "PoliteSession",
    "meta", "meta_any", "normalise_date", "strip_accents", "word_match",
    "classify_by_keyword", "detect_country", "detect_in_list", "make_url_id",
    "article_to_row", "articles_to_rows", "write_csv", "read_rows",
    "dedupe_rows", "write_rows", "sort_rows_by_date", "asdict",
]

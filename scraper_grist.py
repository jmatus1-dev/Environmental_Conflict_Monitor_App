"""
scraper_grist.py - Grist (https://grist.org)

Grist is a US-based environmental-journalism non-profit covering climate,
energy, environmental justice, indigenous rights and food/agriculture
across the United States (and some global coverage). It adds the North
American angle the project's other sources lack — US mining disputes,
pipeline fights, indigenous land conflicts (e.g. Standing Rock-type
stories), and contamination cases.

Grist is a GENERAL climate outlet (lots of climate-culture, health and
solutions content), so we filter STRICTLY to keep only environmental
CONFLICT stories.

Discovery strategy
------------------
WordPress site. Article URLs follow:
    /<category>/<slug>/    e.g. /indigenous/environmental-defenders-remain-...
Content is organised by topic category. We seed from the conflict-relevant
categories, collect /<category>/<slug>/ links, drop the junk (/about/,
/support-us/, /tag/, /author/, section fronts), dedupe and visit each.

Per-article extraction
----------------------
Standard Open Graph + article:* meta tags (og:title, og:description,
article:published_time in clean ISO format, author).

robots.txt note
---------------
Grist's robots.txt blocks only */republish/ (a republishing widget) and is
otherwise an empty Disallow (allow all). PoliteSession's robots check works
correctly here.

Shared machinery lives in common.py; this file keeps only the site specifics.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import common
from common import (
    Article, PoliteSession, meta, meta_any, normalise_date, make_url_id,
    classify_by_keyword, detect_country, detect_in_list,
)

# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://grist.org"
SOURCE_NAME = "Grist"
DEFAULT_COUNTRY = "United States"   # US outlet; most stories are US, but we
                                    # detect other countries when present.

SEED_URLS: list[str] = [
    f"{BASE_URL}/indigenous/",          # land defenders / indigenous rights
    f"{BASE_URL}/energy/",              # oil, gas, pipelines, mining
    f"{BASE_URL}/food-and-agriculture/",
    f"{BASE_URL}/equity/",             # environmental justice
    f"{BASE_URL}/politics/",
    f"{BASE_URL}/accountability/",
    f"{BASE_URL}/regulation/",
]

# Category path segments that begin a real article URL.
KNOWN_CATEGORIES = {
    "indigenous", "energy", "food-and-agriculture", "equity", "politics",
    "accountability", "regulation", "solutions", "extreme-weather",
    "cities", "culture", "health", "science", "business", "economics",
    "article", "international",
}


# ---------------------------------------------------------------------------
# Keyword dictionaries (ENGLISH) + place lists
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": ["mining", "mine", "miners", "mineral", "minerals",
                   "gold mining", "copper", "lithium", "coal mining",
                   "iron ore", "tailings", "uranium mining", "rare earth"],
        "oil_gas": ["oil spill", "petroleum", "crude oil", "oil pipeline",
                    "gas pipeline", "pipeline", "fracking", "natural gas",
                    "drilling", "oil drilling", "lng", "refinery"],
        "logging_deforestation": ["deforestation", "logging", "old-growth",
                                  "clear-cut", "clearcut", "timber",
                                  "forest loss", "tree-felling"],
        "infrastructure": ["hydroelectric", "hydroelectric dam", "dam project",
                           "mega-dam", "oil pipeline", "gas pipeline",
                           "pipeline project"],
        "agriculture": ["agribusiness", "cattle ranching", "factory farm",
                        "animal agriculture", "monoculture", "feedlot",
                        "cafo", "plantation"],
        "protected_areas": ["national park", "protected area", "wildlife refuge",
                            "tribal land", "reservation land", "sacred land",
                            "public lands", "wilderness"],
    },
    "event_type": {
        "protest": ["protest", "protests", "blockade", "demonstration",
                    "rally", "encampment", "water protectors", "standoff"],
        "legal": ["lawsuit", "sued", "court ruling", "ruling", "supreme court",
                  "injunction", "legal challenge", "permit revoked",
                  "permit denied", "settlement"],
        "violence": ["land defender", "land defenders", "environmental defender",
                     "environmental defenders", "activists killed",
                     "indigenous leader killed", "death threat", "targeted",
                     "assassinated", "criminalized"],
        "pollution": ["contamination", "contaminated", "polluted", "pollution",
                      "spill", "leak", "toxic", "toxic waste", "poisoned",
                      "forever chemicals", "pfas", "coal ash"],
        "displacement": ["displacement", "displaced", "forced eviction",
                         "evicted", "relocated", "forced relocation"],
        "consultation_dispute": ["free prior", "prior consultation",
                                 "consent", "tribal consultation",
                                 "without consent", "treaty rights"],
        "enforcement_action": ["raid", "seized", "fined", "violation",
                               "epa enforcement", "crackdown", "arrested"],
    },
    "environmental_issue": {
        "oil spill": ["oil spill", "crude oil spill", "pipeline rupture",
                      "pipeline leak"],
        "water contamination": ["water contamination", "contaminated water",
                                "polluted water", "pfas", "forever chemicals",
                                "lead contamination", "coal ash"],
        "deforestation": ["deforestation", "old-growth logging", "clear-cut",
                          "forest loss", "logging"],
        "biodiversity loss": ["endangered species", "extinction",
                              "biodiversity", "habitat loss",
                              "endangered species act"],
        "land grabbing": ["land grab", "land seizure", "broken treaty",
                          "stolen land"],
        "illegal mining": ["illegal mining", "unpermitted mining"],
        "air pollution": ["air pollution", "smog", "emissions", "toxic air"],
        "wildfire": ["wildfire", "wildfires", "forest fire", "megafire"],
    },
}

# Country detection. US first (home country), then the rest of the Americas.
COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "United States": ["united states", "u.s.", "america", "american",
                      "texas", "california", "alaska", "appalachia",
                      "wyoming", "montana", "north dakota", "louisiana",
                      "gulf coast", "standing rock", "navajo", "arizona",
                      "new mexico", "west virginia", "minnesota", "michigan",
                      "florida", "oregon", "washington state"],
    "Canada":   ["canada", "canadian", "alberta", "british columbia",
                 "first nations", "ontario", "tar sands", "oil sands"],
    "Mexico":   ["mexico", "mexican", "oaxaca", "chiapas", "sonora"],
    "Brazil":   ["brazil", "brazilian", "amazon", "yanomami", "cerrado"],
    "Colombia": ["colombia", "colombian", "cerrejon"],
    "Peru":     ["peru", "peruvian", "madre de dios"],
    "Ecuador":  ["ecuador", "ecuadorian", "yasuni"],
    "Bolivia":  ["bolivia", "bolivian"],
    "Chile":    ["chile", "chilean", "atacama"],
    "Argentina": ["argentina", "argentine", "vaca muerta"],
    "Venezuela": ["venezuela", "venezuelan", "orinoco"],
    "Guatemala": ["guatemala", "guatemalan"],
    "Honduras": ["honduras", "honduran"],
    "Panama":   ["panama", "panamanian"],
}

# US states + a few cross-border regions (for region_department).
SUBNATIONAL_REGIONS = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas",
    "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming",
    # Cross-border regions that appear in some pieces
    "Alberta", "British Columbia", "Ontario",
]


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# Article URLs: /<category>/<slug>/ where <category> is a known section and
# <slug> has at least one hyphen (multi-word headline).
_ARTICLE_PATH_RE = re.compile(
    r"^/([a-z0-9-]+)/([a-z0-9]+(?:-[a-z0-9]+)+)/?$"
)

_NON_ARTICLE_SLUGS = {"about", "support-us", "advertising", "subscribe",
                      "membership", "events", "all-topics", "articles"}


def _is_article_url(path: str) -> bool:
    m = _ARTICLE_PATH_RE.match(path)
    if not m:
        return False
    category, slug = m.group(1), m.group(2)
    if category not in KNOWN_CATEGORIES:
        return False
    if slug in _NON_ARTICLE_SLUGS:
        return False
    return True


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Article URLs found on a category page."""
    logging.info("Discovering articles on %s", seed_url)
    r = session.get(seed_url)
    if r is None:
        return set()
    soup = BeautifulSoup(r.text, "lxml")
    found: set[str] = set()
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE_URL, a["href"])
        parsed = urlparse(url)
        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue
        if not _is_article_url(parsed.path):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if not clean.endswith("/"):
            clean += "/"
        found.add(clean)
    logging.info("  -> %d candidate article URLs", len(found))
    return found


# ---------------------------------------------------------------------------
# Article parsing
# ---------------------------------------------------------------------------

def parse_article(url: str, html: str) -> Optional[Article]:
    soup = BeautifulSoup(html, "lxml")

    title = meta_any(soup, ["og:title", "twitter:title"]) or (
        soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta_any(soup, ["og:description", "twitter:description",
                                  "description"]) or ""
    published = meta(soup, "article:published_time")
    author = meta(soup, "author") or ""

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    url_slug = urlparse(url).path.replace("-", " ").replace("/", " ")
    blob = " ".join([title, description, url_slug]).lower()

    sector = classify_by_keyword(blob, KEYWORDS["sector"])
    event_type = classify_by_keyword(blob, KEYWORDS["event_type"])
    env_issue = classify_by_keyword(blob, KEYWORDS["environmental_issue"])
    country = detect_country(blob, COUNTRY_KEYWORDS, DEFAULT_COUNTRY)
    region = detect_in_list(title + " " + description, SUBNATIONAL_REGIONS)

    notes_parts = []
    if not sector:
        notes_parts.append("No sector matched; manual review.")
    if not event_type:
        notes_parts.append("No event_type matched; manual review.")
    if author:
        notes_parts.append(f"Author: {author}")

    return Article(
        article_id=make_url_id("grist", url),
        source=SOURCE_NAME,
        article_title=title,
        article_url=url,
        date_published=normalise_date(published),
        country=country,
        region_department=region,
        locality="",
        latitude="",
        longitude="",
        sector=sector,
        actor_company="",
        community_actor="",
        event_type=event_type,
        environmental_issue=env_issue,
        source_text_excerpt=description[:500],
        extraction_method="metadata + keyword rule",
        notes=" | ".join(notes_parts),
        extra={},
    )


# ---------------------------------------------------------------------------
# Relevance filter (STRICT - general climate outlet)
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Grist is a GENERAL climate outlet (lots of climate-culture, health,
    solutions content), so we filter strictly to keep only environmental
    CONFLICT stories.

    Keep only if ANY of:
      - a sector AND (event_type OR environmental_issue) were detected, OR
      - a real environmental_issue was detected on its own, OR
      - a violence event was detected (our violence keywords only match
        environmental-/land-defender targeting, which is itself a conflict
        signal even when no industry sector is named).

    This drops the climate-therapist / explainer / soft-solutions pieces
    while keeping mining, pipeline, land-defender and contamination stories.
    """
    has_sector = bool(article.sector)
    has_event = bool(article.event_type)
    has_issue = bool(article.environmental_issue)

    if has_sector and (has_event or has_issue):
        return True
    if has_issue:
        return True
    if article.event_type == "violence":
        return True
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    return PoliteSession(BASE_URL, delay=delay,
                         accept_language="en-US,en;q=0.9")


def crawl(max_articles: int, session: PoliteSession) -> list[Article]:
    candidate_urls: set[str] = set()
    for seed in SEED_URLS:
        candidate_urls.update(discover_article_urls(seed, session))
        if len(candidate_urls) > max_articles * 5:
            logging.info("Collected enough candidates (%d) — stopping early.",
                         len(candidate_urls))
            break

    logging.info("Total unique candidate articles: %d", len(candidate_urls))

    kept: list[Article] = []
    for i, url in enumerate(sorted(candidate_urls), 1):
        if len(kept) >= max_articles:
            break
        logging.info("[%d/%d] %s", i, len(candidate_urls), url)
        r = session.get(url)
        if r is None:
            continue
        article = parse_article(url, r.text)
        if article is None:
            continue
        if is_relevant(article):
            kept.append(article)
            logging.info("  kept (sector=%s, event=%s, country=%s)",
                         article.sector or "-", article.event_type or "-",
                         article.country or "-")
        else:
            logging.debug("  filtered out")

    kept.sort(key=lambda a: a.date_published or "0000-00-00", reverse=True)
    return kept


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Scrape Grist on its own.")
    p.add_argument("--out", default="grist.csv")
    p.add_argument("--max-articles", type=int, default=50)
    p.add_argument("--delay", type=float, default=1.0)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    arts = crawl(args.max_articles, make_session(args.delay))
    common.write_csv(arts, args.out)
    logging.info("Wrote %d articles to %s", len(arts), args.out)

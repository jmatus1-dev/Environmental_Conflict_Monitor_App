"""
scraper_guardian.py - The Guardian (https://www.theguardian.com)

The Guardian is a paywall-free international newspaper with a dedicated
environment desk. Because it is a GENERAL newspaper covering the whole
world, we filter aggressively and apply an Americas-only gate to stay
within the project's scope.

This is the common.py-based version (matches scraper_dialogue_earth.py,
scraper_grist.py, etc.) so it plugs straight into run_all.py via
make_session() + crawl(max_articles, session). The classification logic,
keyword lists, blocklist, URL regex and Americas gate are identical to the
earlier standalone build — only the plumbing changed.

Discovery strategy
------------------
Real Guardian article URLs:  /<section>/<YYYY>/<mon>/<DD>/<slug>
e.g. /environment/2024/dec/15/cerrejon-mine-colombia-protest
Archive pages like /environment/mining/2026/may/29/all are rejected by the
URL filter (the slug must be a multi-word hyphenated headline, not "all"),
as are galleries, videos, interactives and podcasts.

Three-part keep rule (is_relevant):
  (A) blocklist: title/description must NOT contain an off-topic phrase
      (plane crash, deportation, sports, election...);
  (B) environmental-conflict signal: a strong sector + event/issue, OR an
      /environment/ URL + strong sector, OR a real environmental_issue; AND
  (C) Americas gate: an Americas country was detected.

robots.txt note
---------------
The Guardian permits crawling; PoliteSession's robots check works as-is.

Shared machinery lives in common.py; this file keeps only Guardian specifics.
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
    word_match, classify_by_keyword, detect_country,
)

# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.theguardian.com"
SOURCE_NAME = "The Guardian"
DEFAULT_COUNTRY = ""   # blank => not detected => dropped by the Americas gate


SEED_URLS: list[str] = [
    # --- Global environment topic pages (broad, recent coverage) ---
    f"{BASE_URL}/environment/mining",
    f"{BASE_URL}/environment/oil",
    f"{BASE_URL}/environment/oil-spills",
    f"{BASE_URL}/environment/deforestation",
    f"{BASE_URL}/environment/forests",
    f"{BASE_URL}/environment/amazon-rainforest",
    f"{BASE_URL}/environment/water",
    f"{BASE_URL}/environment/pollution",
    f"{BASE_URL}/environment/indigenous-peoples",
    f"{BASE_URL}/environment/conservation",

    # --- South America: per-country world pages ---
    f"{BASE_URL}/world/colombia",
    f"{BASE_URL}/world/peru",
    f"{BASE_URL}/world/brazil",
    f"{BASE_URL}/world/ecuador",
    f"{BASE_URL}/world/bolivia",
    f"{BASE_URL}/world/chile",
    f"{BASE_URL}/world/venezuela",
    f"{BASE_URL}/world/argentina",
    f"{BASE_URL}/world/guyana",
    f"{BASE_URL}/world/paraguay",
    f"{BASE_URL}/world/uruguay",

    # --- Central America & Mexico ---
    f"{BASE_URL}/world/mexico",
    f"{BASE_URL}/world/guatemala",
    f"{BASE_URL}/world/honduras",
    f"{BASE_URL}/world/nicaragua",
    f"{BASE_URL}/world/panama",

    # --- Regional umbrella pages ---
    f"{BASE_URL}/world/americas",
    f"{BASE_URL}/global-development/americas",
]


# ---------------------------------------------------------------------------
# Keyword dictionaries (ENGLISH)
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": [
            "mining", "mine", "miner", "miners", "mineral", "minerals",
            "gold mining", "copper mining", "lithium mining", "iron ore",
            "open-pit", "open pit", "tailings", "dredge", "dredges",
            "gold rush", "wildcat", "garimpo", "garimpeiros",
        ],
        "oil_gas": [
            "oil spill", "oil drilling", "petroleum", "crude oil",
            "oil pipeline", "oil block", "fracking", "hydraulic fracturing",
            "natural gas", "gas pipeline", "hydrocarbons", "drilling",
            "ecopetrol", "petrobras", "chevron", "pdvsa",
        ],
        "logging_deforestation": [
            "deforestation", "logging", "illegal logging", "clear-cut",
            "clearcut", "clearing", "forest loss", "deforested", "loggers",
            "desmatamento", "tala ilegal",
        ],
        # NOTE: deliberately narrow. Generic words like "port", "airport",
        # "highway", "aircraft", "railway" were removed because they matched
        # plane-crash, deportation and traffic stories with no environmental
        # angle. We keep only terms that denote an environmental-impact
        # project (dams, pipelines cutting through land, etc.).
        "infrastructure": [
            "hydroelectric", "hydroelectric dam", "hydropower",
            "dam project", "mega-dam", "megadam", "oil pipeline",
            "gas pipeline", "pipeline project",
        ],
        "agriculture": [
            "agribusiness", "cattle ranching", "monoculture", "ranchers",
            "palm oil", "oil palm", "soy", "soya", "soybean",
            "plantation", "agricultural expansion",
        ],
        "protected_areas": [
            "national park", "protected area", "nature reserve",
            "indigenous reserve", "indigenous territory", "indigenous land",
            "wildlife sanctuary", "biosphere reserve",
        ],
    },
    "event_type": {
        "protest": [
            "protest", "protests", "protested", "protesters", "blockade",
            "blockades", "march", "marched", "demonstration", "rally",
            "strike", "stand-off", "standoff",
        ],
        "legal": [
            "lawsuit", "sued", "court ruling", "ruling", "verdict",
            "supreme court", "constitutional court", "injunction", "high court",
            "environmental licence", "environmental license", "legal rights",
            "permit revoked", "permit suspended",
        ],
        # NOTE: narrowed to environmental-conflict violence. Bare "killed"/
        # "attack" matched plane crashes, racism arrests and unrelated crime,
        # so they were removed. We keep terms that name violence against
        # land/environmental defenders specifically.
        "violence": [
            "land defender", "land defenders", "environmental defender",
            "environmental defenders", "activists killed", "activist killed",
            "indigenous leader killed", "death threat", "death threats",
            "massacre", "assassinated", "assassination",
        ],
        "pollution": [
            "contamination", "contaminated", "polluted", "pollution",
            "spill", "leak", "leaked", "discharge", "toxic waste",
            "river polluted", "poisoned", "sewage",
        ],
        "displacement": [
            "displacement", "displaced", "forced eviction", "evicted",
            "relocated", "evacuated", "forced from their homes",
        ],
        "consultation_dispute": [
            "free prior", "prior consultation", "free, prior and informed",
            "fpic", "indigenous consultation", "without consent",
            "consultation dispute",
        ],
        "enforcement_action": [
            "raid", "seized", "seizure", "operation", "police operation",
            "arrested", "detained", "investigation", "task force", "crackdown",
            "environmental police", "destroyed equipment",
        ],
    },
    "environmental_issue": {
        "oil spill": [
            "oil spill", "crude oil spill", "petroleum spill",
            "pipeline rupture", "pipeline leak",
        ],
        "mercury contamination": [
            "mercury", "mercury contamination", "mercury poisoning",
            "mercury pollution",
        ],
        "deforestation": [
            "deforestation", "forest loss", "illegal logging", "logging",
            "tree-felling", "tree felling", "clear-cut", "clearcut",
            "forest clearance",
        ],
        "river pollution": [
            "river polluted", "river contamination", "polluted river",
            "river pollution", "watershed contamination",
        ],
        "biodiversity loss": [
            "biodiversity", "biodiversity loss", "extinction",
            "endangered species", "species loss",
        ],
        "land grabbing": [
            "land grab", "land grabbing", "land seizure",
            "stolen land", "land dispossession",
        ],
        "illegal mining": [
            "illegal mining", "wildcat mining", "unauthorized mining",
            "informal mining", "garimpo",
        ],
        "water scarcity": [
            "water scarcity", "drought", "water shortage", "dried up",
        ],
        "wildfire": [
            "wildfire", "forest fire", "blaze", "fires burned",
        ],
    },
}


# ---------------------------------------------------------------------------
# Country detection (Americas only). Order = priority (first match wins).
# Each list is intentionally specific to ONE country. Ambiguous terms that
# belong to two countries (e.g. "patagonia" spans Chile AND Argentina) are
# deliberately omitted to avoid mis-tagging.
# ---------------------------------------------------------------------------

COUNTRY_KEYWORDS: dict[str, list[str]] = {
    # --- South America ---
    "Colombia":  ["colombia", "colombian", "bogota", "bogotá", "medellin",
                  "medellín", "cali", "cartagena", "cerrejon", "cerrejón",
                  "antioquia", "choco", "chocó"],
    "Peru":      ["peru", "peruvian", "lima", "loreto", "madre de dios",
                  "ucayali", "cusco", "cajamarca", "amarakaeri"],
    "Ecuador":   ["ecuador", "ecuadorean", "ecuadorian", "quito",
                  "guayaquil", "yasuni", "yasuní", "sucumbios", "sucumbíos",
                  "galapagos", "galápagos"],
    "Bolivia":   ["bolivia", "bolivian", "la paz", "cochabamba", "beni",
                  "pando", "tipnis"],
    "Brazil":    ["brazil", "brazilian", "brasil", "amazonas",
                  "manaus", "pará", "rondonia", "rondônia", "mato grosso",
                  "yanomami", "sao paulo", "são paulo", "rio de janeiro",
                  "atlantic forest", "cerrado", "munduruku"],
    "Venezuela": ["venezuela", "venezuelan", "caracas", "orinoco",
                  "arco minero"],
    "Chile":     ["chile", "chilean", "santiago", "atacama",
                  "antofagasta"],
    "Argentina": ["argentina", "argentinian", "argentine", "buenos aires",
                  "gran chaco", "vaca muerta", "valdes", "valdés", "mendoza"],
    "Guyana":    ["guyana", "guyanese", "georgetown"],
    "Suriname":  ["suriname", "surinamese", "paramaribo"],
    "Paraguay":  ["paraguay", "paraguayan", "asuncion", "asunción"],
    "Uruguay":   ["uruguay", "uruguayan", "montevideo"],

    # --- Central America & Caribbean ---
    "Mexico":    ["mexico", "mexican", "oaxaca", "chiapas",
                  "yucatan", "yucatán", "sonora", "sierra tarahumara"],
    "Guatemala": ["guatemala", "guatemalan"],
    "Honduras":  ["honduras", "honduran", "tegucigalpa"],
    "Nicaragua": ["nicaragua", "nicaraguan", "managua"],
    "Costa Rica":["costa rica", "costa rican"],
    "Panama":    ["panama", "panamanian", "cobre panama"],
    "El Salvador":["el salvador", "salvadoran"],
    "Belize":    ["belize", "belizean"],

    # --- North America ---
    "United States": ["united states", "u.s.", "texas", "california",
                      "alaska", "appalachia", "wyoming", "montana",
                      "north dakota", "louisiana", "gulf coast",
                      "san antonio", "standing rock"],
    "Canada":    ["canada", "canadian", "ottawa", "alberta",
                  "british columbia", "first nations"],
}

# A set of every Americas country name, used by the Americas-only gate.
AMERICAS_COUNTRIES = set(COUNTRY_KEYWORDS.keys())


# ---------------------------------------------------------------------------
# Blocklist: phrases that almost always signal a NON-environmental story.
# If any of these appears in the title/description, the article is rejected
# outright, even if a sector/event keyword also matched. This catches the
# false positives we saw: plane crashes, airport arrests, ICE deportations,
# sports sponsorship protests, election disputes, etc.
# ---------------------------------------------------------------------------

BLOCKLIST: list[str] = [
    # aviation / transport accidents
    "plane crash", "aircraft", "airplane", "jet crash", "airport",
    "netjets", "flight",
    # immigration / deportation
    "deport", "deported", "deportation", "ice agents", "ice raid",
    "asylum", "migrant", "immigration",
    # crime / social stories unrelated to environment
    "racism", "racist", "racial slur", "sexual", "assault charge",
    # sports
    "world cup", "olympics", "fifa", "match against", "tournament",
    "sponsor",
    # pure electoral / geopolitical
    "election", "ballot", "presidential race", "referendum",
    # misc non-environmental
    "drug trafficking", "cartel war", "kidnapping",
]


def _hits_blocklist(blob: str) -> Optional[str]:
    """Return the first blocklist phrase found in `blob`, else None.
    `blob` is expected to be lowercased."""
    for phrase in BLOCKLIST:
        if word_match(blob, phrase):
            return phrase
    return None


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# A real Guardian article URL looks like:
#     /<section>/<YYYY>/<mon>/<DD>/<slug>
# where <slug> always contains at least one hyphen (multiple words). That
# last point lets us reject archive pages whose final segment is "all".
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

_ARTICLE_PATH_RE = re.compile(
    r"^/[a-z0-9-]+(?:/[a-z0-9-]+)*"          # one or more section words
    r"/\d{4}/(?:" + _MONTHS + r")/\d{1,2}"   # /YYYY/mon/DD
    r"/[a-z0-9]+(?:-[a-z0-9]+)+/?$"          # slug: must have >=1 hyphen
)

# Content types we never want, even at an article-shaped URL.
_NON_ARTICLE_SEGMENTS = {
    "gallery", "ng-interactive", "audio", "video", "picture",
    "live", "interactive", "podcast", "cartoon",
}


def _is_article_url(path: str) -> bool:
    """True only for real article paths. Rejects archive (/all) pages,
    galleries, videos, interactives, podcasts, etc."""
    if not _ARTICLE_PATH_RE.match(path):
        return False
    segments = [s for s in path.split("/") if s]
    for seg in segments:
        if seg in _NON_ARTICLE_SEGMENTS:
            return False
    if segments and segments[-1] == "all":
        return False
    return True


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Return real article URLs found on a section/country page."""
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
    section = meta(soup, "article:section") or ""

    author = meta_any(soup, ["article:author", "author"]) or ""
    if not author:
        a_tag = soup.find("a", attrs={"rel": "author"})
        if a_tag:
            author = a_tag.get_text(strip=True)

    published = meta_any(soup, ["article:published_time", "og:updated_time"])
    if not published:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            published = time_tag["datetime"]

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    # Blob: title + description + section + URL slug (slug often names the
    # country even when the headline doesn't).
    url_slug = urlparse(url).path.replace("-", " ").replace("/", " ")
    blob = " ".join([title, description, section, url_slug]).lower()

    sector = classify_by_keyword(blob, KEYWORDS["sector"])
    event_type = classify_by_keyword(blob, KEYWORDS["event_type"])
    env_issue = classify_by_keyword(blob, KEYWORDS["environmental_issue"])
    country = detect_country(blob, COUNTRY_KEYWORDS, DEFAULT_COUNTRY)

    notes_parts = []
    if not sector:
        notes_parts.append("No sector matched; manual review.")
    if not event_type:
        notes_parts.append("No event_type matched; manual review.")
    if not country:
        notes_parts.append("Country not detected (likely non-Americas).")
    if author:
        notes_parts.append(f"Author: {author}")
    if section:
        notes_parts.append(f"Section: {section}")

    return Article(
        article_id=make_url_id("guardian", url),
        source=SOURCE_NAME,
        article_title=title,
        article_url=url,
        date_published=normalise_date(published),
        country=country,
        region_department="",   # left to match_admin.py downstream
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
# Relevance filter (STRICT) + Americas-only gate + blocklist
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Keep only genuine environmental-conflict stories set in the Americas.

    THREE conditions must ALL hold:
      (A) blocklist: title/description must NOT contain an off-topic phrase;
      (B) environmental-conflict signal, via EITHER:
            - a STRONG sector AND (event_type OR environmental_issue), OR
            - the URL is in /environment/ AND has a strong sector, OR
            - a real environmental_issue was detected;
      (C) Americas gate: an Americas country was detected.
    """
    blob = f"{article.article_title} {article.source_text_excerpt}".lower()

    # (A) blocklist overrides everything
    blocked = _hits_blocklist(blob)
    if blocked:
        logging.debug("  blocked by '%s'", blocked)
        return False

    # (B) environmental-conflict signal
    STRONG_SECTORS = {
        "mining", "oil_gas", "logging_deforestation",
        "agriculture", "protected_areas",
    }
    in_environment = "/environment/" in article.article_url
    has_strong_sector = article.sector in STRONG_SECTORS
    has_event = bool(article.event_type)
    has_issue = bool(article.environmental_issue)

    signal_ok = (
        (has_strong_sector and (has_event or has_issue))
        or (in_environment and has_strong_sector)
        or has_issue
    )
    if not signal_ok:
        return False

    # (C) Americas-only gate
    if article.country not in AMERICAS_COUNTRIES:
        return False

    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    return PoliteSession(BASE_URL, delay=delay,
                         accept_language="en-GB,en;q=0.9,es;q=0.7")


def crawl(max_articles: int, session: PoliteSession) -> list[Article]:
    candidate_urls: set[str] = set()
    for seed in SEED_URLS:
        candidate_urls.update(discover_article_urls(seed, session))
        # Over-collect: the strict filter + Americas gate drop a lot.
        if len(candidate_urls) > max_articles * 8:
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
            logging.debug("  filtered out (sector=%s, country=%s)",
                          article.sector or "-", article.country or "-")

    kept.sort(key=lambda a: a.date_published or "0000-00-00", reverse=True)
    return kept


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Scrape The Guardian on its own.")
    p.add_argument("--out", default="guardian.csv")
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

"""
scraper_dialogue_earth.py - Dialogue Earth (https://dialogue.earth/en)

Dialogue Earth (formerly China Dialogue) is a non-profit environmental
journalism outlet with a dedicated Latin America desk. It covers mining,
oil, dams, deforestation and indigenous land rights across the region —
with particular depth on Chinese-financed extraction projects in the
Amazon and Andes, a niche the other sources cover poorly. Every article
is environmental, so we filter loosely (like Mongabay) and add an
Americas-only gate to stay within the project's geographic scope.

Discovery strategy
------------------
Article URLs follow the pattern:
    /en/<category>/<slug>/        e.g. /en/pollution/latin-america-rare-earths/
Category fronts (single path segment) are the seed pages:
    /en/nature/  /en/pollution/  /en/forests/  /en/water/  /en/energy/  ...
plus the Latin America topic page /en/topics/latin-america/.

We crawl each seed, collect /en/<category>/<slug>/ links, drop the junk
(/feed/, /wp-json/, /tag/, /about/, section fronts), dedupe and visit each.

Per-article extraction
----------------------
Dialogue Earth exposes standard Open Graph + article:* meta tags
(og:title, og:description, article:published_time, author) — same format
the other scrapers read.

robots.txt note
---------------
Dialogue Earth's robots.txt ALLOWS full crawling (only /cms/wp-admin/ is
disallowed, which we never touch). However, the file contains two
`User-agent: *` groups, which Python's standard RobotFileParser
mis-parses, incorrectly reporting every URL as disallowed. To avoid that
false block, this scraper disables PoliteSession's automatic robots check
and instead hard-codes the one real rule: never fetch /cms/wp-admin/.
The polite request delay is still enforced.

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
    Article, PoliteSession, meta_any, normalise_date, make_url_id,
    classify_by_keyword, detect_country, detect_in_list,
)

# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://dialogue.earth"
SOURCE_NAME = "Dialogue Earth"
DEFAULT_COUNTRY = ""               # global site; Americas gate applied below

# Category fronts + the Latin America topic page. These are the seeds we
# crawl for article links. We pick the categories most likely to carry
# environmental-conflict stories.
SEED_URLS: list[str] = [
    f"{BASE_URL}/en/topics/latin-america/",
    f"{BASE_URL}/en/nature/",
    f"{BASE_URL}/en/pollution/",
    f"{BASE_URL}/en/forests/",
    f"{BASE_URL}/en/water/",
    f"{BASE_URL}/en/energy/",
    f"{BASE_URL}/en/food/",
    f"{BASE_URL}/en/ocean/",
    f"{BASE_URL}/en/business/",
    f"{BASE_URL}/en/justice/",
    f"{BASE_URL}/en/climate/",
]

# Category path segments that appear right after /en/. Used to recognise
# real article URLs (/en/<category>/<slug>/).
KNOWN_CATEGORIES = {
    "nature", "pollution", "forests", "water", "energy", "food", "ocean",
    "business", "justice", "climate", "opinion",
}


# ---------------------------------------------------------------------------
# Keyword dictionaries (ENGLISH) + place lists
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": ["mining", "mine", "miners", "mineral", "minerals",
                   "gold mining", "copper", "lithium", "rare earth",
                   "rare earths", "iron ore", "tailings", "dredge",
                   "garimpo", "wildcat"],
        "oil_gas": ["oil spill", "petroleum", "crude oil", "oil pipeline",
                    "oil block", "fracking", "natural gas", "drilling",
                    "hydrocarbons", "petrobras", "ecopetrol"],
        "logging_deforestation": ["deforestation", "logging", "illegal logging",
                                  "forest loss", "clear-cut", "tree-felling",
                                  "deforested"],
        "infrastructure": ["hydroelectric", "hydroelectric dam", "dam project",
                           "mega-dam", "megadam", "oil pipeline",
                           "gas pipeline", "pipeline project"],
        "agriculture": ["agribusiness", "cattle ranching", "monoculture",
                        "palm oil", "soy", "soybean", "plantation"],
        "protected_areas": ["national park", "protected area", "nature reserve",
                            "indigenous reserve", "indigenous territory",
                            "wildlife sanctuary"],
    },
    "event_type": {
        "protest": ["protest", "protests", "blockade", "demonstration",
                    "rally", "strike"],
        "legal": ["lawsuit", "sued", "court ruling", "ruling", "supreme court",
                  "injunction", "legal rights", "permit revoked", "verdict"],
        "violence": ["land defender", "land defenders", "environmental defender",
                     "activists killed", "indigenous leader killed",
                     "death threat", "massacre", "assassinated"],
        "pollution": ["contamination", "contaminated", "polluted", "pollution",
                      "spill", "leak", "toxic waste", "poisoned"],
        "displacement": ["displacement", "displaced", "forced eviction",
                         "evicted", "relocated"],
        "consultation_dispute": ["free prior", "prior consultation",
                                 "free, prior and informed", "without consent"],
        "enforcement_action": ["raid", "seized", "seizure", "police operation",
                               "arrested", "crackdown", "task force"],
    },
    "environmental_issue": {
        "oil spill": ["oil spill", "crude oil spill", "pipeline rupture"],
        "mercury contamination": ["mercury", "mercury contamination",
                                  "mercury poisoning"],
        "deforestation": ["deforestation", "forest loss", "illegal logging",
                          "logging", "tree-felling", "clear-cut"],
        "river pollution": ["river polluted", "river contamination",
                            "polluted river", "river pollution"],
        "biodiversity loss": ["biodiversity", "extinction",
                              "endangered species", "biodiversity loss"],
        "land grabbing": ["land grab", "land grabbing", "land seizure"],
        "illegal mining": ["illegal mining", "wildcat mining", "garimpo"],
        "wildfire": ["wildfire", "forest fire", "blaze"],
    },
}

# Americas country detection. Specific, non-overlapping terms only.
COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "Colombia": ["colombia", "colombian", "bogota", "bogotá", "cerrejon",
                 "cerrejón", "antioquia"],
    "Peru":     ["peru", "peruvian", "loreto", "madre de dios", "ucayali",
                 "cusco", "cajamarca"],
    "Ecuador":  ["ecuador", "ecuadorian", "ecuadorean", "yasuni", "yasuní",
                 "sucumbios", "sucumbíos"],
    "Bolivia":  ["bolivia", "bolivian", "beni", "pando", "tipnis"],
    "Brazil":   ["brazil", "brazilian", "brasil", "manaus", "rondônia",
                 "rondonia", "mato grosso", "yanomami", "munduruku",
                 "atlantic forest", "cerrado"],
    "Venezuela": ["venezuela", "venezuelan", "orinoco", "esequibo",
                  "arco minero"],
    "Chile":    ["chile", "chilean", "atacama", "antofagasta"],
    "Argentina": ["argentina", "argentinian", "argentine", "gran chaco",
                  "vaca muerta"],
    "Guyana":   ["guyana", "guyanese"],
    "Suriname": ["suriname", "surinamese"],
    "Paraguay": ["paraguay", "paraguayan"],
    "Uruguay":  ["uruguay", "uruguayan"],
    "Mexico":   ["mexico", "mexican", "oaxaca", "chiapas", "sonora"],
    "Guatemala": ["guatemala", "guatemalan"],
    "Honduras": ["honduras", "honduran"],
    "Nicaragua": ["nicaragua", "nicaraguan"],
    "Costa Rica": ["costa rica", "costa rican"],
    "Panama":   ["panama", "panamanian", "cobre panama"],
    "United States": ["united states", "u.s.", "texas", "alaska", "appalachia",
                      "gulf coast"],
    "Canada":   ["canada", "canadian", "alberta", "british columbia",
                 "first nations"],
}

AMERICAS_COUNTRIES = set(COUNTRY_KEYWORDS.keys())

SUBNATIONAL_REGIONS = [
    # Colombia
    "Amazonas", "Antioquia", "Caquetá", "Cauca", "Chocó", "Córdoba",
    "Guainía", "Guaviare", "Meta", "Nariño", "Putumayo", "Tolima",
    "Valle del Cauca", "Vaupés", "Vichada", "La Guajira",
    # Peru
    "Loreto", "Madre de Dios", "Ucayali", "Cusco", "Junín", "Pasco",
    "Huánuco", "San Martín", "Cajamarca",
    # Ecuador
    "Sucumbíos", "Orellana", "Napo", "Pastaza", "Morona Santiago",
    "Zamora Chinchipe", "Esmeraldas",
    # Bolivia
    "Beni", "Pando", "Santa Cruz", "La Paz", "Cochabamba",
    # Brazil
    "Acre", "Rondônia", "Roraima", "Pará", "Mato Grosso", "Maranhão",
    "Tocantins",
]


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# Real article: /en/<category>/<slug>/  where <category> is a known section
# and <slug> contains at least one hyphen (multi-word headline slug). This
# rejects section fronts like /en/energy/ (no slug) and junk like /en/feed/.
_ARTICLE_PATH_RE = re.compile(
    r"^/en/([a-z0-9-]+)/([a-z0-9]+(?:-[a-z0-9]+)+)/?$"
)

# Path segments that are never articles.
_NON_ARTICLE_SECONDS = {"feed", "wp-json", "tag", "author", "page", "amp"}


def _is_article_url(path: str) -> bool:
    m = _ARTICLE_PATH_RE.match(path)
    if not m:
        return False
    category, slug = m.group(1), m.group(2)
    if category not in KNOWN_CATEGORIES:
        return False
    if slug in _NON_ARTICLE_SECONDS:
        return False
    # Reject /feed/ tails etc. that slipped through as the slug.
    if path.rstrip("/").endswith("/feed"):
        return False
    return True


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Article URLs found on a category/topic page."""
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
    published = meta_any(soup, ["article:published_time",
                                "article:modified_time"])
    author = meta_any(soup, ["author"]) or ""

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    # Searchable blob: title + description + URL slug (the slug often names
    # the country/place even when the headline doesn't).
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
    if not country:
        notes_parts.append("Country not detected (likely non-Americas).")
    if author:
        notes_parts.append(f"Author: {author}")

    return Article(
        article_id=make_url_id("dialogue-earth", url),
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
# Relevance filter (loose on environment, strict on geography)
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Dialogue Earth is environmental journalism, so the environmental
    filter is loose. But it's a global site, so we add an Americas-only
    gate to stay within the project's scope.

    Keep the article only if BOTH:
      (A) it was classified with any sector/event/issue; AND
      (B) an Americas country was detected.
    """
    env_ok = bool(article.sector or article.event_type
                  or article.environmental_issue)
    if not env_ok:
        return False
    if article.country not in AMERICAS_COUNTRIES:
        return False
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    """Build a PoliteSession for Dialogue Earth.

    Dialogue Earth's robots.txt allows full crawling (only /cms/wp-admin/
    is disallowed), but its double `User-agent: *` block makes Python's
    RobotFileParser falsely report everything as disallowed. We therefore
    bypass the automatic robots parser and enforce the single real rule
    ourselves (never fetch /cms/wp-admin/), while keeping the polite delay.
    """
    session = PoliteSession(
        BASE_URL, delay=delay,
        accept_language="en-US,en;q=0.9,es;q=0.7",
    )
    # Neutralise the buggy parser: setting _robots to None makes
    # PoliteSession.allowed() return True for everything (see common.py).
    session._robots = None
    logging.info("Dialogue Earth: using manual robots rule (allow all except "
                 "/cms/wp-admin/); site robots.txt permits full crawl.")
    return session


def _allowed(url: str) -> bool:
    """The single real robots.txt rule for Dialogue Earth."""
    return "/cms/wp-admin/" not in url


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
        if not _allowed(url):
            continue
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
    p = argparse.ArgumentParser(description="Scrape Dialogue Earth on its own.")
    p.add_argument("--out", default="dialogue_earth.csv")
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

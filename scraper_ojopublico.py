"""
scraper_ojopublico.py - Ojo Público (https://ojo-publico.com)

Ojo Público is a Peruvian investigative-journalism non-profit with deep,
recurring coverage of environmental crime — illegal gold mining, mercury
contamination, deforestation and land conflict in the Peruvian Amazon
(Madre de Dios, Loreto, Ucayali) and across the Andes. Its pieces are
long-form investigations, so volume is modest but signal density is very
high and squarely on-topic for environmental conflict.

Discovery strategy
------------------
Ojo Público runs on Drupal. Article URLs follow the pattern:
    /<node-id>/<slug>      e.g. /6377/corporate-mechanism-behind-illegal-...
Section pages are plain words: /ambiente, /ambiente/territorio-amazonas, etc.
We crawl the environment + related sections, collect /<id>/<slug> links,
drop the junk (login, register, language switches), dedupe and visit each.

Per-article extraction
----------------------
Ojo Público exposes unusually rich Open Graph + article:* meta tags:
    og:title, og:description           -> title / summary
    article:published_time             -> date (Spanish format, parsed below)
    og:country_name                    -> country (direct signal!)
    article:tag (repeated)             -> topic + place tags, read as
                                          STRUCTURED signals for classification
                                          and region detection
    article:author                     -> author

robots.txt note
---------------
Ojo Público's robots.txt is the standard Drupal one: it blocks only CMS
internals (/core/, /profiles/, /admin/, README files, etc.) — never
articles. No anti-scraping prose, no AI-bot blocks. PoliteSession's robots
check works correctly here, so we use it as-is.

Shared machinery lives in common.py; this file keeps only the site specifics.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import common
from common import (
    Article, PoliteSession, meta, meta_any, make_url_id,
    classify_by_keyword, detect_country, detect_in_list,
)

# ---------------------------------------------------------------------------
# Site configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://ojo-publico.com"
SOURCE_NAME = "Ojo Público"
DEFAULT_COUNTRY = "Peru"   # Ojo Público is Peruvian; most stories are Peru,
                           # but we still detect other countries when present.

SEED_URLS: list[str] = [
    f"{BASE_URL}/ambiente",
    f"{BASE_URL}/ambiente/territorio-amazonas",
    f"{BASE_URL}/ambiente/cop29",
    f"{BASE_URL}/edicion-regional",
    f"{BASE_URL}/sala-del-poder/crimen-organizado",
]


# ---------------------------------------------------------------------------
# article:tag mappings (Ojo Público's editorial tags -> our schema).
# These tags are Spanish. Order = priority (first match wins).
# ---------------------------------------------------------------------------

TAG_TO_SECTOR: dict[str, str] = {
    "mineria ilegal": "mining", "minería ilegal": "mining",
    "oro ilegal": "mining", "mineria": "mining", "minería": "mining",
    "oro": "mining", "petroleo": "oil_gas", "petróleo": "oil_gas",
    "hidrocarburos": "oil_gas", "deforestacion": "logging_deforestation",
    "deforestación": "logging_deforestation",
    "tala ilegal": "logging_deforestation",
    "agroindustria": "agriculture", "palma aceitera": "agriculture",
    "areas protegidas": "protected_areas",
    "áreas protegidas": "protected_areas",
}

TAG_TO_EVENT: dict[str, str] = {
    "conflicto social": "protest", "protesta": "protest",
    "consulta previa": "consultation_dispute",
    "defensores ambientales": "violence",
    "lideres indigenas": "violence", "líderes indígenas": "violence",
    "asesinato": "violence", "narcotrafico": "enforcement_action",
    "narcotráfico": "enforcement_action",
    "lavado de activos": "enforcement_action",
    "contaminacion": "pollution", "contaminación": "pollution",
}

TAG_TO_ENV_ISSUE: dict[str, str] = {
    "oro ilegal": "illegal mining", "mineria ilegal": "illegal mining",
    "minería ilegal": "illegal mining", "mercurio": "mercury contamination",
    "deforestacion": "deforestation", "deforestación": "deforestation",
    "tala ilegal": "deforestation", "biodiversidad": "biodiversity loss",
}


# ---------------------------------------------------------------------------
# Fallback keyword dictionaries (Spanish + English) + place lists
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": ["minería", "mineria", "minero", "oro", "oro ilegal",
                   "mercurio", "draga", "dragas", "garimpo", "mining",
                   "gold mining", "illegal gold"],
        "oil_gas": ["petróleo", "petroleo", "hidrocarburos", "oleoducto",
                    "derrame", "lote petrolero", "oil spill", "drilling"],
        "logging_deforestation": ["deforestación", "deforestacion",
                                  "tala ilegal", "tala", "bosque",
                                  "deforestation", "logging"],
        "infrastructure": ["represa", "hidroeléctrica", "hidroelectrica",
                           "carretera", "dam", "pipeline"],
        "agriculture": ["agroindustria", "palma aceitera", "monocultivo",
                        "ganadería", "soja", "palm oil", "cattle"],
        "protected_areas": ["área protegida", "area protegida",
                            "reserva nacional", "parque nacional",
                            "territorio indígena", "protected area"],
    },
    "event_type": {
        "protest": ["protesta", "bloqueo", "paro", "conflicto social",
                    "protest", "blockade"],
        "legal": ["demanda", "sentencia", "fiscalía", "fiscalia", "fallo",
                  "lawsuit", "ruling"],
        "violence": ["asesinato", "asesinado", "amenaza", "líder indígena",
                     "lider indigena", "defensor ambiental", "killed",
                     "murdered", "land defender"],
        "pollution": ["contaminación", "contaminacion", "derrame",
                      "río contaminado", "pollution", "spill",
                      "contamination"],
        "displacement": ["desplazamiento", "desplazados", "displacement"],
        "consultation_dispute": ["consulta previa", "consentimiento",
                                 "free prior", "prior consultation"],
        "enforcement_action": ["operativo", "incautación", "incautacion",
                               "narcotráfico", "narcotrafico",
                               "lavado de activos", "intervención",
                               "seized", "raid", "crackdown"],
    },
    "environmental_issue": {
        "illegal mining": ["minería ilegal", "mineria ilegal", "oro ilegal",
                           "illegal mining", "illegal gold"],
        "mercury contamination": ["mercurio", "mercury"],
        "oil spill": ["derrame de petróleo", "derrame petrolero", "oil spill"],
        "deforestation": ["deforestación", "deforestacion", "tala ilegal",
                          "deforestation"],
        "river pollution": ["río contaminado", "contaminación del río",
                            "river pollution"],
        "biodiversity loss": ["biodiversidad", "extinción", "biodiversity"],
    },
}

# Country detection. Peru first (it's the home country / most common).
COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "Peru":     ["perú", "peru", "peruano", "peruana", "lima", "loreto",
                 "madre de dios", "ucayali", "cusco", "cajamarca", "puno",
                 "amazonas peruano"],
    "Colombia": ["colombia", "colombiano", "bogotá", "bogota", "antioquia"],
    "Ecuador":  ["ecuador", "ecuatoriano", "yasuní", "yasuni", "sucumbíos"],
    "Bolivia":  ["bolivia", "boliviano", "la paz", "beni", "pando"],
    "Brazil":   ["brasil", "brazil", "amazonas brasileño", "rondônia"],
    "Venezuela": ["venezuela", "venezolano", "orinoco", "arco minero"],
    "Chile":    ["chile", "chileno", "atacama"],
    "Mexico":   ["méxico", "mexico", "mexicano"],
}

SUBNATIONAL_REGIONS = [
    # Peru (primary)
    "Madre de Dios", "Loreto", "Ucayali", "Cusco", "Junín", "Pasco",
    "Huánuco", "San Martín", "Cajamarca", "Puno", "Amazonas", "Apurímac",
    "Ayacucho", "Arequipa",
    # Neighbours that appear in cross-border pieces
    "Sucumbíos", "Orellana", "Napo", "Pastaza",
    "Beni", "Pando", "Santa Cruz",
    "Acre", "Rondônia", "Pará",
    "Antioquia", "Chocó", "Putumayo", "Caquetá", "Amazonas",
]


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# Article URLs: /<node-id>/<slug>  e.g. /6377/corporate-mechanism-behind-...
# The node-id is digits; the slug has at least one hyphen (multi-word).
_ARTICLE_PATH_RE = re.compile(r"^/\d{2,7}/[a-z0-9]+(?:-[a-z0-9]+)+/?$")

# Path prefixes that are never articles.
_NON_ARTICLE_PREFIXES = (
    "/user", "/registro", "/aliados", "/english", "/america",
    "/opinion", "/ojo-bionico",
)


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Article URLs found on a section page."""
    logging.info("Discovering articles on %s", seed_url)
    r = session.get(seed_url)
    if r is None:
        return set()
    soup = BeautifulSoup(r.text, "lxml")
    found: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(href.startswith(p) for p in _NON_ARTICLE_PREFIXES):
            continue
        url = urljoin(BASE_URL, href)
        parsed = urlparse(url)
        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue
        if not _ARTICLE_PATH_RE.match(parsed.path):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if not clean.endswith("/"):
            clean += "/"
        found.add(clean)
    logging.info("  -> %d candidate article URLs", len(found))
    return found


# ---------------------------------------------------------------------------
# Date parsing (Ojo Público's Spanish format)
# ---------------------------------------------------------------------------

# article:published_time looks like:  "Dom, 21/06/2026 - 08:00"
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _parse_ojo_date(raw: Optional[str]) -> str:
    """Turn 'Dom, 21/06/2026 - 08:00' into '2026-06-21'. Falls back to ''."""
    if not raw:
        return ""
    m = _DATE_RE.search(raw)
    if not m:
        return ""
    day, month, year = m.group(1), m.group(2), m.group(3)
    try:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Article parsing
# ---------------------------------------------------------------------------

def _extract_tags(soup) -> list[str]:
    """Read every <meta property='article:tag'> value (lowercased)."""
    tags: list[str] = []
    for t in soup.find_all("meta", attrs={"property": "article:tag"}):
        v = (t.get("content") or "").strip()
        if v:
            tags.append(v.lower())
    return tags


def parse_article(url: str, html: str) -> Optional[Article]:
    soup = BeautifulSoup(html, "lxml")

    title = meta_any(soup, ["og:title", "twitter:title"]) or (
        soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta_any(soup, ["og:description", "twitter:description",
                                  "description"]) or ""
    published_raw = meta(soup, "article:published_time")
    author = meta(soup, "article:author") or ""
    country_meta = meta(soup, "og:country_name") or ""

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    tags = _extract_tags(soup)
    tag_set = set(tags)

    # --- Classify from structured article:tags first ---
    sector = event_type = env_issue = ""
    for tag, label in TAG_TO_SECTOR.items():
        if tag in tag_set:
            sector = label
            break
    for tag, label in TAG_TO_EVENT.items():
        if tag in tag_set:
            event_type = label
            break
    for tag, label in TAG_TO_ENV_ISSUE.items():
        if tag in tag_set:
            env_issue = label
            break

    # --- Country: prefer the og:country_name meta, else detect ---
    country = ""
    cm = country_meta.strip().lower()
    if cm in ("perú", "peru"):
        country = "Peru"
    elif cm:
        # Map other og:country_name values through our detector
        country = detect_country(cm, COUNTRY_KEYWORDS, "")

    # --- Keyword fallback for anything still blank ---
    tag_blob = " ".join(tags)
    url_slug = urlparse(url).path.replace("-", " ").replace("/", " ")
    blob = " ".join([title, description, tag_blob, url_slug]).lower()
    if not sector:
        sector = classify_by_keyword(blob, KEYWORDS["sector"])
    if not event_type:
        event_type = classify_by_keyword(blob, KEYWORDS["event_type"])
    if not env_issue:
        env_issue = classify_by_keyword(blob, KEYWORDS["environmental_issue"])
    if not country:
        country = detect_country(blob, COUNTRY_KEYWORDS, DEFAULT_COUNTRY)

    # --- Region: check the tags too (they often name Peruvian regions) ---
    region = detect_in_list(title + " " + description + " " + tag_blob,
                            SUBNATIONAL_REGIONS)

    notes_parts = []
    if tags:
        notes_parts.append("Tags: " + ", ".join(tags[:8]))
    if not sector:
        notes_parts.append("No sector matched; manual review.")
    if not event_type:
        notes_parts.append("No event_type matched; manual review.")
    if author:
        notes_parts.append(f"Author: {author}")

    return Article(
        article_id=make_url_id("ojopublico", url),
        source=SOURCE_NAME,
        article_title=title,
        article_url=url,
        date_published=_parse_ojo_date(published_raw),
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
        extraction_method="metadata + article tags + keyword rule",
        notes=" | ".join(notes_parts),
        extra={"tags": tags},
    )


# ---------------------------------------------------------------------------
# Relevance filter
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Ojo Público is a general investigative outlet, not environment-only,
    so we filter for an environmental signal. Keep the article if it was
    classified with a sector, event_type, or environmental_issue, OR it
    carries a recognised environmental tag. (Country is essentially always
    an Americas country here, so no separate geo gate is needed — Ojo
    Público is Peruvian and covers the region.)"""
    if article.sector or article.event_type or article.environmental_issue:
        return True
    env_tags = (set(TAG_TO_SECTOR) | set(TAG_TO_EVENT) | set(TAG_TO_ENV_ISSUE)
                | {"ambiente", "amazonía", "amazonia", "biodiversidad"})
    return any(t in env_tags for t in article.extra.get("tags", []))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    return PoliteSession(BASE_URL, delay=delay,
                         accept_language="es-PE,es;q=0.9,en;q=0.7")


def crawl(max_articles: int, session: PoliteSession) -> list[Article]:
    candidate_urls: set[str] = set()
    for seed in SEED_URLS:
        candidate_urls.update(discover_article_urls(seed, session))
        if len(candidate_urls) > max_articles * 4:
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
            logging.debug("  filtered out (tags=%s)",
                          article.extra.get("tags"))

    kept.sort(key=lambda a: a.date_published or "0000-00-00", reverse=True)
    return kept


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Scrape Ojo Público on its own.")
    p.add_argument("--out", default="ojopublico.csv")
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

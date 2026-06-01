"""
scraper_infoamazonia.py - InfoAmazonia (https://infoamazonia.org/es)

Cross-border Amazon-basin environmental journalism covering all nine Amazon
countries. Strong on trans-boundary stories (illegal-mining networks,
basin-wide deforestation). Runs on WordPress, so each article links back to
its editorial taxonomy:

    /es/etiqueta/<slug>/              -> topic tag (mineria, deforestacion...)
    /es/categoria-es/region/<slug>/   -> country/region (colombia, peru...)

We read those slugs as STRUCTURED tags (more reliable than guessing from
text), map them to our schema, then fall back to keyword matching for any
column still blank.

Shared machinery lives in common.py; this file keeps only InfoAmazonia
specifics.
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

BASE_URL = "https://infoamazonia.org"
LANG_PREFIX = "/es"
SOURCE_NAME = "InfoAmazonia"
DEFAULT_COUNTRY = ""   # covers all Amazon countries; no single default

SEED_URLS: list[str] = [
    f"{BASE_URL}{LANG_PREFIX}/noticias/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/mineria/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/mineria-ilegal/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/deforestacion/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/petroleo/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/contaminacion/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/derrame/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/mercurio/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/pueblos-indigenas/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/consulta-previa/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/defensores-ambientales/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/cambio-climatico/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/biodiversidad/",
    f"{BASE_URL}{LANG_PREFIX}/etiqueta/amazonia/",
    f"{BASE_URL}{LANG_PREFIX}/categoria-es/region/colombia/",
    f"{BASE_URL}{LANG_PREFIX}/categoria-es/region/peru/",
    f"{BASE_URL}{LANG_PREFIX}/categoria-es/region/ecuador/",
    f"{BASE_URL}{LANG_PREFIX}/categoria-es/region/bolivia/",
    f"{BASE_URL}{LANG_PREFIX}/categoria-es/region/brasil/",
    f"{BASE_URL}{LANG_PREFIX}/categoria-es/region/venezuela/",
]


# ---------------------------------------------------------------------------
# Slug mappings: InfoAmazonia's editorial taxonomy -> our schema.
# Insertion order encodes priority (earlier entry wins on ties).
# ---------------------------------------------------------------------------

TOPIC_SLUG_TO_SECTOR: dict[str, str] = {
    "mineria": "mining", "mineria-ilegal": "mining", "mineria-legal": "mining",
    "oro": "mining", "petroleo": "oil_gas", "derrame": "oil_gas",
    "derrame-de-petroleo": "oil_gas", "hidrocarburos": "oil_gas", "gas": "oil_gas",
    "deforestacion": "logging_deforestation", "tala-ilegal": "logging_deforestation",
    "bosques": "logging_deforestation", "agricultura": "agriculture",
    "ganaderia": "agriculture", "palma-aceitera": "agriculture", "soja": "agriculture",
    "areas-protegidas": "protected_areas", "parques-nacionales": "protected_areas",
    "reservas-naturales": "protected_areas", "infraestructura": "infrastructure",
    "carreteras": "infrastructure", "represas": "infrastructure",
    "hidroelectricas": "infrastructure",
}

TOPIC_SLUG_TO_EVENT: dict[str, str] = {
    "protestas": "protest", "bloqueos": "protest", "consulta-previa": "consultation_dispute",
    "defensores-ambientales": "violence", "asesinatos": "violence", "violencia": "violence",
    "derechos-humanos": "violence", "conflictos-socioambientales": "protest",
    "justicia-ambiental": "legal", "sentencias": "legal", "licencias-ambientales": "legal",
    "operativos": "enforcement_action", "incautaciones": "enforcement_action",
    "desplazamiento": "displacement", "contaminacion": "pollution", "derrame": "pollution",
}

TOPIC_SLUG_TO_ENV_ISSUE: dict[str, str] = {
    "mineria-ilegal": "illegal mining", "derrame-de-petroleo": "oil spill",
    "derrame": "oil spill", "mercurio": "mercury contamination",
    "tala-ilegal": "deforestation", "deforestacion": "deforestation",
    "incendios": "wildfire", "biodiversidad": "biodiversity loss",
    "extincion": "biodiversity loss", "contaminacion": "river pollution",
    "cambio-climatico": "climate impact",
}

COUNTRY_SLUG: dict[str, str] = {
    "colombia": "Colombia", "peru": "Peru", "ecuador": "Ecuador",
    "bolivia": "Bolivia", "brasil": "Brazil", "venezuela": "Venezuela",
    "guyana": "Guyana", "surinam": "Suriname", "guayana-francesa": "French Guiana",
}


# ---------------------------------------------------------------------------
# Fallback keyword dictionaries + place lists
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": ["minería", "mineria", "minero", "minera", "oro", "mercurio",
                   "concesión minera", "garimpo", "draga", "dragas", "mining"],
        "oil_gas": ["petróleo", "petroleo", "derrame petrolero", "oleoducto",
                    "bloque petrolero", "fracking", "hidrocarburos",
                    "ecopetrol", "petrobras", "oil spill"],
        "logging_deforestation": ["deforestación", "deforestacion", "tala ilegal",
                                  "tala", "desmatamento", "bosque", "selva",
                                  "deforestation"],
        "infrastructure": ["represa", "hidroeléctrica", "carretera", "puerto",
                           "aeropuerto", "infraestructura", "ferrovia", "pipeline",
                           "dam"],
        "agriculture": ["agroindustria", "ganadería", "ganaderia", "monocultivo",
                        "palma aceitera", "soja", "soya", "cattle ranching"],
        "protected_areas": ["parque nacional", "área protegida", "reserva natural",
                            "santuario de fauna", "resguardo indígena",
                            "territorio indígena", "protected area"],
    },
    "event_type": {
        "protest": ["protesta", "bloqueo", "marcha", "paro", "manifestación",
                    "protest", "blockade"],
        "legal": ["demanda", "tutela", "licencia ambiental", "sentencia", "fallo",
                  "corte constitucional", "lawsuit", "ruling"],
        "violence": ["asesinato", "asesinada", "asesinado", "homicidio", "amenaza",
                     "atentado", "killed", "murdered"],
        "pollution": ["contaminación", "derrame", "vertimiento", "río contaminado",
                      "pollution", "spill"],
        "displacement": ["desplazamiento", "desplazados", "displacement"],
        "consultation_dispute": ["consulta previa", "consentimiento libre",
                                 "consulta indígena"],
        "enforcement_action": ["operativo", "incautación", "captura", "detenidos",
                               "fiscalía", "policía ambiental", "seized"],
    },
    "environmental_issue": {
        "oil spill": ["derrame de petróleo", "derrame petrolero", "oil spill"],
        "mercury contamination": ["mercurio", "mercury contamination"],
        "deforestation": ["deforestación", "tala ilegal", "desmatamento",
                          "deforestation"],
        "river pollution": ["río contaminado", "contaminación del río",
                            "river pollution"],
        "biodiversity loss": ["biodiversidad", "extinción", "biodiversity loss"],
        "illegal mining": ["minería ilegal", "garimpo", "illegal mining"],
    },
}

COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "Peru":     ["perú", "peru", "loreto", "madre de dios", "ucayali"],
    "Ecuador":  ["ecuador", "yasuní", "yasuni", "sucumbíos", "sucumbios"],
    "Bolivia":  ["bolivia", "beni", "pando"],
    "Brazil":   ["brasil", "brazil", "manaus", "pará", "rondônia", "rondonia"],
    "Venezuela": ["venezuela", "esequibo"],
    "Colombia": ["colombia", "bogotá", "bogota"],
}

SUBNATIONAL_REGIONS = [
    "Amazonas", "Antioquia", "Arauca", "Atlántico", "Bolívar", "Boyacá",
    "Caldas", "Caquetá", "Casanare", "Cauca", "Cesar", "Chocó", "Córdoba",
    "Cundinamarca", "Guainía", "Guaviare", "Huila", "La Guajira", "Magdalena",
    "Meta", "Nariño", "Norte de Santander", "Putumayo", "Quindío", "Risaralda",
    "San Andrés", "Santander", "Sucre", "Tolima", "Valle del Cauca",
    "Vaupés", "Vichada",
    "Loreto", "Madre de Dios", "Ucayali", "Cusco", "Junín", "Pasco",
    "Huánuco", "San Martín",
    "Sucumbíos", "Orellana", "Napo", "Pastaza", "Morona Santiago",
    "Zamora Chinchipe", "Esmeraldas",
    "Beni", "Pando", "Santa Cruz", "La Paz", "Cochabamba",
    "Acre", "Rondônia", "Roraima", "Pará", "Mato Grosso", "Maranhão",
    "Tocantins",
]


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# Article URL pattern: /es/YYYY/MM/DD/<slug>/
_ARTICLE_PATH_RE = re.compile(r"^/es/(?:19|20)\d{2}/\d{1,2}/\d{1,2}/[a-z0-9-]+/?$")


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Article URLs linked from a tag or category page."""
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
        if not _ARTICLE_PATH_RE.match(parsed.path):
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

_ETIQUETA_LINK_RE = re.compile(r"^https?://infoamazonia\.org/es/etiqueta/([a-z0-9-]+)/?$")
_REGION_LINK_RE = re.compile(
    r"^https?://infoamazonia\.org/es/categoria-es/region/([a-z0-9-]+)/?$")


def _extract_slugs(soup) -> tuple[list[str], list[str]]:
    """Return (topic_slugs, region_slugs) from the links on an article page."""
    topics: set[str] = set()
    regions: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _ETIQUETA_LINK_RE.match(a["href"])
        if m:
            topics.add(m.group(1))
            continue
        m = _REGION_LINK_RE.match(a["href"])
        if m:
            regions.add(m.group(1))
    return sorted(topics), sorted(regions)


def parse_article(url: str, html: str) -> Optional[Article]:
    soup = BeautifulSoup(html, "lxml")

    title = meta_any(soup, ["og:title", "twitter:title"]) or (
        soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta_any(soup, ["og:description", "twitter:description",
                                  "description"]) or ""
    published = meta_any(soup, ["article:published_time", "article:modified_time"])
    author = meta_any(soup, ["article:author", "author", "twitter:data1"]) or ""

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    topic_slugs, region_slugs = _extract_slugs(soup)
    all_slugs = set(topic_slugs)
    region_slug_set = set(region_slugs) | set(topic_slugs)

    sector = event_type = env_issue = ""
    country = DEFAULT_COUNTRY
    for slug, label in TOPIC_SLUG_TO_SECTOR.items():
        if slug in all_slugs:
            sector = label
            break
    for slug, label in TOPIC_SLUG_TO_EVENT.items():
        if slug in all_slugs:
            event_type = label
            break
    for slug, label in TOPIC_SLUG_TO_ENV_ISSUE.items():
        if slug in all_slugs:
            env_issue = label
            break
    for slug, label in COUNTRY_SLUG.items():
        if slug in region_slug_set:
            country = label
            break

    # Pass 2: keyword fallback for anything still blank.
    blob = " ".join([title, description]).lower()
    if not sector:
        sector = classify_by_keyword(blob, KEYWORDS["sector"])
    if not event_type:
        event_type = classify_by_keyword(blob, KEYWORDS["event_type"])
    if not env_issue:
        env_issue = classify_by_keyword(blob, KEYWORDS["environmental_issue"])
    if not country:
        country = detect_country(blob, COUNTRY_KEYWORDS, DEFAULT_COUNTRY)

    region = detect_in_list(title + " " + description, SUBNATIONAL_REGIONS)

    notes_parts = []
    if topic_slugs or region_slugs:
        bits = []
        if topic_slugs:
            bits.append("topics: " + ", ".join(topic_slugs[:6]))
        if region_slugs:
            bits.append("regions: " + ", ".join(region_slugs[:4]))
        notes_parts.append(" / ".join(bits))
    else:
        notes_parts.append("No editorial tags; classified by keywords only.")
    if not sector:
        notes_parts.append("No sector matched; manual review.")
    if not event_type:
        notes_parts.append("No event_type matched; manual review.")
    if not country:
        notes_parts.append("Country not detected; manual review.")
    if author:
        notes_parts.append(f"Author: {author}")

    return Article(
        article_id=make_url_id("infoamazonia", url),
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
        extraction_method="metadata + editorial tags + keyword rule",
        notes=" | ".join(notes_parts),
        extra={"topic_slugs": topic_slugs, "region_slugs": region_slugs},
    )


# ---------------------------------------------------------------------------
# Relevance filter
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """InfoAmazonia is purely environmental journalism, so the filter is loose:
    keep anything classified, anything with a known environmental tag, and (as
    a last resort) anything from the site at all — every article is on-topic
    by definition."""
    if article.sector or article.event_type or article.environmental_issue:
        return True
    env_slugs = (set(TOPIC_SLUG_TO_SECTOR) | set(TOPIC_SLUG_TO_EVENT)
                 | set(TOPIC_SLUG_TO_ENV_ISSUE)
                 | {"amazonia", "biodiversidad", "cambio-climatico"})
    if any(slug in env_slugs for slug in article.extra.get("topic_slugs", [])):
        return True
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    return PoliteSession(BASE_URL, delay=delay)


def crawl(max_articles: int, session: PoliteSession) -> list[Article]:
    candidate_urls: set[str] = set()
    for seed in SEED_URLS:
        candidate_urls.update(discover_article_urls(seed, session))
        if len(candidate_urls) > max_articles * 3:
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
    p = argparse.ArgumentParser(description="Scrape InfoAmazonia on its own.")
    p.add_argument("--out", default="infoamazonia.csv")
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

"""
scraper_mongabay.py - Mongabay Latam (https://es.mongabay.com)

The Spanish-language Latin American edition of Mongabay, a non-profit
environmental-journalism outlet. Every article is environmental, so we filter
little and classify richly. Mongabay's editors tag each article with topic
slugs, exposed as /list/<slug>/ links on the page — we read those as
STRUCTURED tags and only fall back to keywords for blank columns.

Article URL patterns:
    /YYYY/MM/<slug>/
    /custom-story/YYYY/MM/<slug>/

Shared machinery lives in common.py; this file keeps only Mongabay specifics.
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

BASE_URL = "https://es.mongabay.com"
SOURCE_NAME = "Mongabay Latam"
DEFAULT_COUNTRY = ""   # covers all of Latin America

SEED_URLS: list[str] = [
    f"{BASE_URL}/list/mineria/",
    f"{BASE_URL}/list/mineria-ilegal/",
    f"{BASE_URL}/list/deforestacion/",
    f"{BASE_URL}/list/petroleo/",
    f"{BASE_URL}/list/derrame-de-petroleo/",
    f"{BASE_URL}/list/contaminacion/",
    f"{BASE_URL}/list/tala-ilegal/",
    f"{BASE_URL}/list/agricultura/",
    f"{BASE_URL}/list/ganaderia/",
    f"{BASE_URL}/list/cambio-climatico/",
    f"{BASE_URL}/list/biodiversidad/",
    f"{BASE_URL}/list/medioambiente/",
    f"{BASE_URL}/list/pueblos-indigenas/",
    f"{BASE_URL}/list/defensores-ambientales/",
    f"{BASE_URL}/list/derechos-humanos/",
    f"{BASE_URL}/list/conflictos-socioambientales/",
    f"{BASE_URL}/list/colombia/",
    f"{BASE_URL}/list/peru/",
    f"{BASE_URL}/list/ecuador/",
    f"{BASE_URL}/list/bolivia/",
    f"{BASE_URL}/list/brasil/",
    f"{BASE_URL}/list/venezuela/",
    f"{BASE_URL}/list/amazonia/",
    f"{BASE_URL}/list/latinoamerica/",
]


# ---------------------------------------------------------------------------
# Topic-slug mappings (Mongabay's taxonomy -> our schema). Order = priority.
# ---------------------------------------------------------------------------

TOPIC_SLUG_TO_SECTOR: dict[str, str] = {
    "mineria": "mining", "mineria-ilegal": "mining", "mineria-legal": "mining",
    "petroleo": "oil_gas", "derrame-de-petroleo": "oil_gas",
    "hidrocarburos": "oil_gas", "fracking": "oil_gas",
    "deforestacion": "logging_deforestation", "tala-ilegal": "logging_deforestation",
    "bosques": "logging_deforestation", "agricultura": "agriculture",
    "ganaderia": "agriculture", "palma-aceitera": "agriculture", "soja": "agriculture",
    "areas-protegidas": "protected_areas", "parques-nacionales": "protected_areas",
    "reservas-naturales": "protected_areas", "infraestructura": "infrastructure",
    "carreteras": "infrastructure", "represas": "infrastructure",
    "hidroelectricas": "infrastructure",
}

TOPIC_SLUG_TO_EVENT: dict[str, str] = {
    "protestas": "protest", "consulta-previa": "consultation_dispute",
    "defensores-ambientales": "violence", "asesinatos": "violence",
    "derechos-humanos": "violence", "conflictos-socioambientales": "protest",
    "justicia-ambiental": "legal", "sentencias": "legal",
    "licencias-ambientales": "legal", "operativos": "enforcement_action",
    "incautaciones": "enforcement_action", "desplazamiento": "displacement",
    "contaminacion": "pollution",
}

TOPIC_SLUG_TO_ENV_ISSUE: dict[str, str] = {
    "deforestacion": "deforestation", "tala-ilegal": "deforestation",
    "derrame-de-petroleo": "oil spill", "mercurio": "mercury contamination",
    "contaminacion": "river pollution", "biodiversidad": "biodiversity loss",
    "extincion": "biodiversity loss", "mineria-ilegal": "illegal mining",
    "cambio-climatico": "climate impact", "incendios": "wildfire",
}

TOPIC_SLUG_TO_COUNTRY: dict[str, str] = {
    "colombia": "Colombia", "peru": "Peru", "ecuador": "Ecuador",
    "bolivia": "Bolivia", "brasil": "Brazil", "venezuela": "Venezuela",
    "argentina": "Argentina", "chile": "Chile", "guatemala": "Guatemala",
    "mexico": "Mexico", "paraguay": "Paraguay", "uruguay": "Uruguay",
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
                     "atentado", "líder social asesinado", "killed", "murdered"],
        "pollution": ["contaminación", "derrame", "vertimiento", "río contaminado",
                      "pollution", "spill"],
        "displacement": ["desplazamiento", "desplazados", "displacement", "displaced"],
        "consultation_dispute": ["consulta previa", "consentimiento libre",
                                 "free prior", "consulta indígena"],
        "enforcement_action": ["operativo", "incautación", "captura", "detenidos",
                               "fiscalía", "policía ambiental",
                               "destrucción de dragas", "seized"],
    },
    "environmental_issue": {
        "oil spill": ["derrame de petróleo", "derrame petrolero", "oil spill"],
        "mercury contamination": ["mercurio", "mercury contamination"],
        "deforestation": ["deforestación", "tala ilegal", "desmatamento",
                          "deforestation"],
        "river pollution": ["río contaminado", "contaminación del río",
                            "river pollution"],
        "biodiversity loss": ["biodiversidad", "extinción", "biodiversity loss"],
        "land grabbing": ["acaparamiento de tierras", "despojo", "land grab"],
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

_ARTICLE_PATH_RE = re.compile(r"^/(?:custom-story/)?(?:19|20)\d{2}/\d{1,2}/[a-z0-9-]+/?$")


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Article URLs found on a topic listing page."""
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

_LIST_LINK_RE = re.compile(r"^https?://es\.mongabay\.com/list/([a-z0-9-]+)/?$")


def _extract_topic_slugs(soup) -> list[str]:
    """Pull /list/<slug>/ tag URLs (Mongabay's editorial classifications)."""
    slugs: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _LIST_LINK_RE.match(a["href"])
        if m:
            slugs.add(m.group(1))
    return sorted(s for s in slugs if not s.startswith("all"))


def parse_article(url: str, html: str) -> Optional[Article]:
    soup = BeautifulSoup(html, "lxml")

    title = meta_any(soup, ["og:title", "twitter:title"]) or (
        soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta_any(soup, ["og:description", "twitter:description",
                                  "description"]) or ""
    published = meta_any(soup, ["article:published_time", "article:modified_time"])
    author = meta_any(soup, ["twitter:data1", "author"]) or ""
    section = meta_any(soup, ["article:section"]) or ""

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    topic_slugs = _extract_topic_slugs(soup)
    slug_set = set(topic_slugs)

    sector = event_type = env_issue = ""
    country = DEFAULT_COUNTRY
    for slug, label in TOPIC_SLUG_TO_SECTOR.items():
        if slug in slug_set:
            sector = label
            break
    for slug, label in TOPIC_SLUG_TO_EVENT.items():
        if slug in slug_set:
            event_type = label
            break
    for slug, label in TOPIC_SLUG_TO_ENV_ISSUE.items():
        if slug in slug_set:
            env_issue = label
            break
    for slug, label in TOPIC_SLUG_TO_COUNTRY.items():
        if slug in slug_set:
            country = label
            break

    blob = " ".join([title, description, section]).lower()
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
    if topic_slugs:
        notes_parts.append("Topic tags: " + ", ".join(topic_slugs[:8]))
    else:
        notes_parts.append("No topic tags found; classified by keywords only.")
    if not sector:
        notes_parts.append("No sector matched; manual review.")
    if not event_type:
        notes_parts.append("No event_type matched; manual review.")
    if not country:
        notes_parts.append("Country not detected; manual review.")
    if author:
        notes_parts.append(f"Author: {author}")

    return Article(
        article_id=make_url_id("mongabay", url),
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
        extraction_method="metadata + topic tags + keyword rule",
        notes=" | ".join(notes_parts),
        extra={"topic_slugs": topic_slugs},
    )


# ---------------------------------------------------------------------------
# Relevance filter
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Loose filter: every Mongabay article is environmental journalism. Keep
    it if any column was classified, or if it carries a recognised
    environmental topic slug."""
    if article.sector or article.event_type or article.environmental_issue:
        return True
    env_slugs = (set(TOPIC_SLUG_TO_SECTOR) | set(TOPIC_SLUG_TO_EVENT)
                 | set(TOPIC_SLUG_TO_ENV_ISSUE)
                 | {"medioambiente", "biodiversidad", "cambio-climatico", "amazonia"})
    return any(slug in env_slugs for slug in article.extra.get("topic_slugs", []))


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
            logging.debug("  filtered out (slugs=%s)",
                          article.extra.get("topic_slugs"))

    kept.sort(key=lambda a: a.date_published or "0000-00-00", reverse=True)
    return kept


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Scrape Mongabay Latam on its own.")
    p.add_argument("--out", default="mongabay.csv")
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

"""
scraper_elespectador.py - El Espectador (https://www.elespectador.com)

Colombian general-interest newspaper. Most stories are NOT environmental, so
we discover from environment sections + topical tag pages and filter
aggressively. Per-article metadata comes from <meta> tags (Open Graph,
article:*, and the publisher's cXenseParse:* tags), which are far more stable
than the visible HTML. Classification is a transparent keyword rule.

All the shared machinery (Article, PoliteSession, CSV writing, the text
helpers) now lives in common.py. This file keeps only what is specific to
El Espectador.

Run it on its own for debugging:
    python scraper_elespectador.py --max-articles 20 --out elespectador.csv
Normally you'd run run_all.py instead.
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

BASE_URL = "https://www.elespectador.com"
SOURCE_NAME = "El Espectador"
DEFAULT_COUNTRY = "Colombia"  # Colombian paper; we still try to detect when an
                              # article is actually about another country.

# Seed pages we crawl to find article URLs. Section pages give the freshest
# environmental coverage; tag pages give topical depth per sector.
SEED_URLS: list[str] = [
    f"{BASE_URL}/ambiente/",
    f"{BASE_URL}/ambiente/amazonas/",
    f"{BASE_URL}/ambiente/bibo/",
    f"{BASE_URL}/ambiente/blog-el-rio/",
    f"{BASE_URL}/tags/mineria/",
    f"{BASE_URL}/tags/mineria-ilegal/",
    f"{BASE_URL}/tags/mineria-legal/",
    f"{BASE_URL}/tags/mineria-en-colombia/",
    f"{BASE_URL}/tags/petroleo/",
    f"{BASE_URL}/tags/derrame-de-petroleo/",
    f"{BASE_URL}/tags/fracking/",
    f"{BASE_URL}/tags/deforestacion/",
    f"{BASE_URL}/tags/tala-ilegal/",
    f"{BASE_URL}/tags/amazonia/",
    f"{BASE_URL}/tags/contaminacion-ambiental/",
    f"{BASE_URL}/tags/contaminacion-ambiental-en-colombia/",
    f"{BASE_URL}/tags/mercurio/",
    f"{BASE_URL}/tags/consulta-previa/",
    f"{BASE_URL}/tags/comunidades-indigenas/",
    f"{BASE_URL}/tags/pueblos-indigenas/",
    f"{BASE_URL}/tags/parques-nacionales/",
    f"{BASE_URL}/tags/licencia-ambiental/",
]


# ---------------------------------------------------------------------------
# Keyword dictionaries (rule-based classifier) + place lists
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": [
            "minería", "mineria", "minero", "minera", "oro", "mercurio",
            "concesión minera", "concesion minera", "garimpo", "garimpeiros",
            "draga", "dragas", "mining",
        ],
        "oil_gas": [
            "petróleo", "petroleo", "derrame de petroleo", "derrame petrolero",
            "oleoducto", "bloque petrolero", "fracking", "gas natural",
            "hidrocarburos", "ecopetrol", "petrobras", "oil spill",
        ],
        "logging_deforestation": [
            "deforestación", "deforestacion", "tala ilegal", "tala",
            "desmatamento", "bosque", "selva", "logging", "deforestation",
        ],
        "infrastructure": [
            "represa", "hidroeléctrica", "hidroelectrica", "carretera",
            "vía", "puerto", "aeropuerto", "infraestructura", "ferrovia",
            "pipeline", "highway", "dam",
        ],
        "agriculture": [
            "agroindustria", "ganadería", "ganaderia", "monocultivo",
            "palma africana", "palma aceitera", "soja", "soya",
            "agriculture", "cattle ranching",
        ],
        "protected_areas": [
            "parque nacional", "área protegida", "area protegida",
            "reserva natural", "santuario de fauna", "resguardo indígena",
            "resguardo indigena", "territorio indígena", "protected area",
        ],
    },
    "event_type": {
        "protest": [
            "protesta", "bloqueo", "marcha", "paro", "manifestación",
            "manifestacion", "protest", "blockade",
        ],
        "legal": [
            "demanda", "tutela", "licencia ambiental", "sentencia",
            "fallo", "corte constitucional", "lawsuit", "ruling",
        ],
        "violence": [
            "asesinato", "asesinada", "asesinado", "homicidio", "amenaza",
            "atentado", "líder social asesinado", "lider social asesinado",
            "killed", "murdered",
        ],
        "pollution": [
            "contaminación", "contaminacion", "derrame", "vertimiento",
            "río contaminado", "rio contaminado", "pollution", "spill",
        ],
        "displacement": [
            "desplazamiento", "desplazados", "displacement", "displaced",
        ],
        "consultation_dispute": [
            "consulta previa", "consentimiento libre", "free prior",
            "consulta indígena", "consulta indigena",
        ],
        "enforcement_action": [
            "operativo", "incautación", "incautacion", "captura", "detenidos",
            "fiscalía", "fiscalia", "policía ambiental", "policia ambiental",
            "destrucción de", "destruccion de", "seized",
        ],
    },
    "environmental_issue": {
        "oil spill": ["derrame de petróleo", "derrame de petroleo",
                      "derrame petrolero", "oil spill"],
        "mercury contamination": ["mercurio", "contaminación por mercurio",
                                  "contaminacion por mercurio",
                                  "mercury contamination"],
        "deforestation": ["deforestación", "deforestacion", "tala ilegal",
                          "desmatamento", "deforestation"],
        "river pollution": ["río contaminado", "rio contaminado",
                            "contaminación del río", "contaminacion del rio",
                            "river pollution"],
        "biodiversity loss": ["biodiversidad", "extinción", "extincion",
                              "biodiversity loss"],
        "land grabbing": ["acaparamiento de tierras", "despojo", "land grab"],
        "illegal mining": ["minería ilegal", "mineria ilegal",
                           "garimpo", "illegal mining"],
    },
}

COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "Peru": ["perú", "peru", "lima", "loreto", "madre de dios", "ucayali"],
    "Ecuador": ["ecuador", "quito", "guayaquil", "yasuní", "yasuni",
                "sucumbíos", "sucumbios"],
    "Bolivia": ["bolivia", "la paz", "santa cruz", "beni", "pando"],
    "Brazil":  ["brasil", "brazil", "amazonas brasileño", "amazonas brasileno",
                "manaus", "pará", "rondônia", "rondonia"],
    "Venezuela": ["venezuela", "caracas", "amazonas venezolano"],
}

COLOMBIAN_DEPARTMENTS = [
    "Amazonas", "Antioquia", "Arauca", "Atlántico", "Bolívar", "Boyacá",
    "Caldas", "Caquetá", "Casanare", "Cauca", "Cesar", "Chocó", "Córdoba",
    "Cundinamarca", "Guainía", "Guaviare", "Huila", "La Guajira", "Magdalena",
    "Meta", "Nariño", "Norte de Santander", "Putumayo", "Quindío", "Risaralda",
    "San Andrés", "Santander", "Sucre", "Tolima", "Valle del Cauca",
    "Vaupés", "Vichada",
]


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# Article URLs are like /ambiente/algun-titulo/ . Exclude tag/author/archive/
# multimedia/special pages.
_ARTICLE_PATH_RE = re.compile(
    r"^/(?!tags/|autores/|archivo/|multimedia/|opinion/caricaturistas/|"
    r"para-ti/|ee-play/|terminos/|newsletters/|suscripciones?/|servicios/)"
    r"[a-z0-9-]+(/[a-z0-9-]+){1,4}/?$"
)


def discover_article_urls(seed_url: str, session: PoliteSession) -> list[str]:
    """Article URLs found on a section/tag page, in DOM order (newest first)."""
    logging.info("Discovering articles on %s", seed_url)
    r = session.get(seed_url)
    if r is None:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    found: list[str] = []
    seen: set[str] = set()
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
        if clean not in seen:
            seen.add(clean)
            found.append(clean)
    logging.info("  -> %d candidate article URLs", len(found))
    return found


# ---------------------------------------------------------------------------
# Article parsing
# ---------------------------------------------------------------------------

def _article_id_from(url: str, cxense_id: Optional[str]) -> str:
    if cxense_id:
        return f"elespectador:{cxense_id}"
    return make_url_id("elespectador", url)


def parse_article(url: str, html: str) -> Optional[Article]:
    """Turn one El Espectador article page into an Article."""
    soup = BeautifulSoup(html, "lxml")

    title = meta_any(soup, ["og:title", "twitter:title", "cXenseParse:title"]) \
        or (soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta_any(soup, [
        "og:description", "twitter:description", "description",
        "cXenseParse:description",
    ]) or ""
    section = meta_any(soup, ["article:section", "cXenseParse:esp-section"]) or ""
    author = meta_any(soup, ["cXenseParse:author", "article:author", "author"]) or ""
    published = meta_any(soup, [
        "article:published_time", "cXenseParse:publishtime",
        "cXenseParse:esp-modified_time",
    ])
    raw_keywords_str = meta_any(soup, [
        "cXenseParse:keywords", "news_keywords", "keywords",
    ]) or ""
    raw_keywords = [k.strip() for k in re.split(r"[,;]", raw_keywords_str) if k.strip()]
    cxense_id = meta(soup, "cXenseParse:articleid")

    if not title:
        logging.debug("No title for %s; skipping.", url)
        return None

    # Searchable text blob. We deliberately do NOT pull the article body, to
    # stay within fair use and avoid breaking on Premium-locked pages.
    blob = " ".join([title, description, raw_keywords_str, section]).lower()

    sector = classify_by_keyword(blob, KEYWORDS["sector"])
    event_type = classify_by_keyword(blob, KEYWORDS["event_type"])
    env_issue = classify_by_keyword(blob, KEYWORDS["environmental_issue"])
    country = detect_country(blob, COUNTRY_KEYWORDS, DEFAULT_COUNTRY)
    region = detect_in_list(title + " " + description + " " + raw_keywords_str,
                            COLOMBIAN_DEPARTMENTS)

    notes_parts = []
    if "premium" in blob or meta(soup, "article:content_tier") == "locked":
        notes_parts.append("Premium/locked article — full body not scraped.")
    if not sector:
        notes_parts.append("No sector matched by keyword rules; manual review.")
    if not event_type:
        notes_parts.append("No event_type matched; manual review.")
    if author:
        notes_parts.append(f"Author: {author}")
    if section:
        notes_parts.append(f"Section: {section}")

    return Article(
        article_id=_article_id_from(url, cxense_id),
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
        extra={"raw_keywords": raw_keywords},
    )


# ---------------------------------------------------------------------------
# Relevance filter
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Keep an article when EITHER the rules tagged a sector AND an event/issue,
    OR it lives in /ambiente/ AND has a sector tag. Keeps weather/pet/zoo
    stories out while letting real environmental coverage through."""
    in_ambiente = "/ambiente/" in article.article_url
    has_sector = bool(article.sector)
    has_event_or_issue = bool(article.event_type or article.environmental_issue)
    if has_sector and has_event_or_issue:
        return True
    if in_ambiente and has_sector:
        return True
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    return PoliteSession(BASE_URL, delay=delay,
                         accept_language="es-CO,es;q=0.9,en;q=0.7")


def crawl(max_articles: int, session: PoliteSession) -> list[Article]:
    """Discover, fetch and keep up to `max_articles` relevant articles,
    newest first."""
    candidate_urls: list[str] = []
    seen_urls: set[str] = set()
    for seed in SEED_URLS:
        for url in discover_article_urls(seed, session):
            if url not in seen_urls:
                seen_urls.add(url)
                candidate_urls.append(url)
        if len(candidate_urls) > max_articles * 4:
            logging.info("Collected enough candidates (%d) — stopping early.",
                         len(candidate_urls))
            break

    logging.info("Total unique candidate articles: %d", len(candidate_urls))

    kept: list[Article] = []
    for i, url in enumerate(candidate_urls, 1):
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
            logging.info("  kept (sector=%s, event=%s)",
                         article.sector or "-", article.event_type or "-")
        else:
            logging.debug("  filtered out")

    kept.sort(key=lambda a: a.date_published or "0000-00-00", reverse=True)
    return kept


# ---------------------------------------------------------------------------
# Standalone entry point (handy for debugging just this one source)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Scrape El Espectador on its own.")
    p.add_argument("--out", default="elespectador.csv")
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

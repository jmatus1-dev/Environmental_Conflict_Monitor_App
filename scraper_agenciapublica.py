"""
scraper_agenciapublica.py - Agência Pública (https://apublica.org)

Agência Pública is a Brazilian investigative-journalism non-profit. It is a
GENERAL investigative outlet (politics, corruption, rights) with a strong
socio-environmental beat covering Amazon deforestation, illegal mining,
land conflict, agribusiness and indigenous rights. Because it is general
(not environment-only), we filter STRICTLY: an article is kept only if it
shows a real environmental signal.

Agência Pública fills the project's biggest gap — Brazil, the largest media
market in the Amazon basin — in Portuguese, which the existing
Spanish-language sources don't cover.

Discovery strategy
------------------
WordPress site. Article URLs follow:
    /YYYY/MM/<slug>/        e.g. /2026/05/no-dia-do-agro-setor-passa-boiada-...
Content is organised under /editorial/<topic>/ sections; we seed from the
environmental + related ones, collect /YYYY/MM/<slug>/ links, drop the junk
(/autor/, /tag/, /editorial/, /page/), dedupe and visit each.

Per-article extraction
----------------------
Standard Open Graph + article:* meta tags (og:title, og:description,
article:published_time in clean ISO format, author).

robots.txt note
---------------
Agência Pública's robots.txt is EMPTY (HTTP 200, zero bytes) — no crawl
restrictions at all, so everything is permitted. PoliteSession's robots
check passes cleanly.

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

BASE_URL = "https://apublica.org"
SOURCE_NAME = "Agência Pública"
DEFAULT_COUNTRY = "Brazil"   # Brazilian outlet; most stories are Brazil, but
                             # we still detect other countries when present.

SEED_URLS: list[str] = [
    f"{BASE_URL}/editorial/socioambiental/",   # the main environment beat
    f"{BASE_URL}/editorial/clima/",
    f"{BASE_URL}/editorial/empresas/",         # corporations / supply chains
    f"{BASE_URL}/editorial/violencia/",        # land-defender violence
    f"{BASE_URL}/tag/meio-ambiente/",
    f"{BASE_URL}/tag/amazonia/",
    f"{BASE_URL}/tag/desmatamento/",
    f"{BASE_URL}/tag/mineracao/",
    f"{BASE_URL}/tag/povos-indigenas/",
    f"{BASE_URL}/tag/garimpo/",
]


# ---------------------------------------------------------------------------
# Keyword dictionaries (PORTUGUESE-first, with some English) + place lists
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, dict[str, list[str]]] = {
    "sector": {
        "mining": ["mineração", "mineracao", "garimpo", "garimpeiros",
                   "garimpeiro", "ouro", "minério", "minerio", "mineradora",
                   "draga", "dragas", "mining", "gold"],
        "oil_gas": ["petróleo", "petroleo", "petrobras", "oleoduto",
                    "exploração de óleo", "gás natural", "fracking",
                    "oil spill", "drilling"],
        "logging_deforestation": ["desmatamento", "desflorestamento",
                                  "extração de madeira", "madeireira",
                                  "tala", "deforestation", "logging"],
        "infrastructure": ["hidrelétrica", "hidreletrica", "barragem",
                           "usina", "rodovia", "ferrogrão", "ferrograo",
                           "dam", "pipeline"],
        "agriculture": ["agronegócio", "agronegocio", "pecuária", "pecuaria",
                        "boiada", "soja", "monocultura", "agropecuária",
                        "palm oil", "cattle ranching"],
        "protected_areas": ["unidade de conservação", "terra indígena",
                            "terra indigena", "reserva", "parque nacional",
                            "área protegida", "protected area"],
    },
    "event_type": {
        "protest": ["protesto", "bloqueio", "manifestação", "manifestacao",
                    "ocupação", "protest", "blockade"],
        "legal": ["ação judicial", "processo", "liminar", "stf", "justiça",
                  "sentença", "lawsuit", "ruling"],
        "violence": ["assassinato", "assassinado", "morto", "ameaça",
                     "ameaçado", "líder indígena", "lider indigena",
                     "defensor ambiental", "massacre", "killed", "murdered"],
        "pollution": ["contaminação", "contaminacao", "poluição", "poluicao",
                      "derramamento", "rio contaminado", "mercúrio",
                      "mercurio", "pollution", "contamination", "spill"],
        "displacement": ["deslocamento", "expulsão", "expulsao", "despejo",
                         "displacement", "displaced"],
        "consultation_dispute": ["consulta prévia", "consulta previa",
                                 "consentimento", "free prior"],
        "enforcement_action": ["operação", "operacao", "apreensão",
                               "apreensao", "fiscalização", "fiscalizacao",
                               "ibama", "polícia federal", "policia federal",
                               "seized", "raid", "crackdown"],
    },
    "environmental_issue": {
        "deforestation": ["desmatamento", "desflorestamento", "deforestation"],
        "illegal mining": ["garimpo", "garimpo ilegal", "mineração ilegal",
                           "mineracao ilegal", "illegal mining"],
        "mercury contamination": ["mercúrio", "mercurio", "mercury"],
        "oil spill": ["derramamento de óleo", "derrame de petróleo",
                      "oil spill"],
        "river pollution": ["rio contaminado", "contaminação do rio",
                            "river pollution"],
        "biodiversity loss": ["biodiversidade", "extinção", "extincao",
                              "espécie ameaçada", "biodiversity"],
        "land grabbing": ["grilagem", "grilagem de terras", "land grab",
                          "land grabbing"],
        "wildfire": ["incêndio", "incendio", "queimada", "queimadas",
                     "wildfire", "fire"],
    },
}

# Country detection. Brazil first (home country).
COUNTRY_KEYWORDS: dict[str, list[str]] = {
    "Brazil":   ["brasil", "brazil", "brasileiro", "brasileira", "amazônia",
                 "amazonia", "pará", "para", "rondônia", "rondonia",
                 "mato grosso", "roraima", "acre", "manaus", "yanomami",
                 "munduruku", "cerrado", "pantanal", "maranhão"],
    "Peru":     ["peru", "peruano", "loreto", "madre de dios", "ucayali"],
    "Colombia": ["colômbia", "colombia", "colombiano", "bogotá"],
    "Ecuador":  ["equador", "ecuador", "yasuní", "yasuni"],
    "Bolivia":  ["bolívia", "bolivia", "boliviano", "beni", "pando"],
    "Venezuela": ["venezuela", "venezuelano", "orinoco"],
    "Colombia2": [],  # placeholder unused
}
COUNTRY_KEYWORDS.pop("Colombia2", None)

SUBNATIONAL_REGIONS = [
    # Brazilian states (primary focus)
    "Acre", "Amapá", "Amazonas", "Pará", "Rondônia", "Roraima", "Tocantins",
    "Maranhão", "Mato Grosso", "Mato Grosso do Sul", "Goiás", "Bahia",
    "Minas Gerais", "São Paulo", "Pará",
    # Cross-border neighbours
    "Loreto", "Madre de Dios", "Ucayali",
    "Sucumbíos", "Orellana",
    "Beni", "Pando",
    "Putumayo", "Amazonas", "Vaupés",
]


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

# Article URLs: /YYYY/MM/<slug>/  with a multi-word (hyphenated) slug.
_ARTICLE_PATH_RE = re.compile(
    r"^/(?:20)\d{2}/\d{1,2}/[a-z0-9]+(?:-[a-z0-9]+)+/?$"
)

_NON_ARTICLE_PREFIXES = (
    "/autor", "/tag", "/editorial", "/page", "/tipo", "/podcast",
    "/assine", "/arquivo", "/trabalhe",
)


def discover_article_urls(seed_url: str, session: PoliteSession) -> set[str]:
    """Article URLs found on a section/tag page."""
    logging.info("Discovering articles on %s", seed_url)
    r = session.get(seed_url)
    if r is None:
        return set()
    soup = BeautifulSoup(r.text, "lxml")
    found: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        url = urljoin(BASE_URL, href)
        parsed = urlparse(url)
        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue
        if any(parsed.path.startswith(p) for p in _NON_ARTICLE_PREFIXES):
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
        article_id=make_url_id("agenciapublica", url),
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
# Relevance filter (STRICT - general outlet, must show environmental signal)
# ---------------------------------------------------------------------------

def is_relevant(article: Article) -> bool:
    """Agência Pública is a GENERAL investigative outlet, so we filter
    strictly to keep only environmental-conflict stories.

    Keep only if EITHER:
      - a sector AND (event_type OR environmental_issue) were detected
        (strong signal), OR
      - a real environmental_issue was detected on its own (deforestation,
        illegal mining, mercury, etc.).

    This drops the outlet's politics/corruption/elections coverage while
    keeping the socio-environmental investigations.
    """
    has_sector = bool(article.sector)
    has_event = bool(article.event_type)
    has_issue = bool(article.environmental_issue)

    if has_sector and (has_event or has_issue):
        return True
    if has_issue:
        return True
    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def make_session(delay: float = 1.0) -> PoliteSession:
    return PoliteSession(BASE_URL, delay=delay,
                         accept_language="pt-BR,pt;q=0.9,en;q=0.7")


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
    p = argparse.ArgumentParser(description="Scrape Agência Pública on its own.")
    p.add_argument("--out", default="agenciapublica.csv")
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

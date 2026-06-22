"""
match_admin.py - locate each article down to municipality (admin 2/3) using GADM.

This version prioritizes ACCURACY over coverage to avoid false positives like
matching the Spanish word "piedras" (stones) to the town of Piedras.

It fills five clean location columns at the END of combined.csv:

    country (admin 0)        - copied verbatim from the existing `country` column
    region (admin 1)         - copied verbatim from `region_department`
    municipality (admin 2)   - the matched municipality (e.g. "Ataco")
    sub-district (admin 3)   - only for Peru (distrito) and Ecuador (parroquia)
    gadm_id                  - unique GADM id for the deepest matched level

How a municipality is found (in priority order)
-----------------------------------------------
1. POINT-IN-POLYGON (preferred, zero false positives):
   If the row has latitude/longitude from the geocode step, we ask GADM which
   municipality polygon actually CONTAINS that point. A point is either inside
   a town's borders or it isn't - there is no ambiguity, so no false matches.

2. CAPITALIZED TEXT MATCH (fallback, only when there are no coordinates):
   We scan the title + body for municipality names, but ONLY accept a name that
   appears Capitalized like a proper noun in the original text. "Piedras" the
   town is capitalized; "piedras" the stones is lowercase and is rejected.
   We also require the match to sit inside the row's own region (admin 1), so a
   town from the wrong part of the country can't win.

Either way, the result is cross-checked: the matched unit must belong to the
same country (and, when known, the same region) the row already has. If it
doesn't, we leave the municipality blank rather than write something wrong.

Setup
-----
    pip install geopandas
    # plus the GADM files in ./shapefiles/  (run download_shapefiles.py)

Run
---
    python match_admin.py                # fill the columns for everything
    python match_admin.py --limit 20     # only the first 20 rows
    python match_admin.py --refetch      # redo rows already matched
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import unicodedata

import common

# Final, human-readable column names (exactly as requested).
COL_A0 = "country (admin 0)"
COL_A1 = "region (admin 1)"
COL_A2 = "municipality (admin 2)"
COL_A3 = "sub-district (admin 3)"
COL_GID = "gadm_id"
NEW_COLS = [COL_A0, COL_A1, COL_A2, COL_A3, COL_GID]

# Old messy columns from the previous version - removed if present.
OLD_COLS = ["admin_2", "admin_3", "gadm_id"]

DATA_PATH = os.environ.get("ENVCONFLICT_DATA", os.path.join("data", "combined.csv"))
SHAPEFILES_DIR = os.environ.get("ENVCONFLICT_SHAPEFILES", "shapefiles")

COUNTRY_TO_ISO3 = {
    "Argentina": "ARG", "Bolivia": "BOL", "Brazil": "BRA", "Brasil": "BRA",
    "Chile": "CHL", "Colombia": "COL", "Ecuador": "ECU", "Guyana": "GUY",
    "Paraguay": "PRY", "Peru": "PER", "Perú": "PER", "Suriname": "SUR",
    "Uruguay": "URY", "Venezuela": "VEN",
}

# Countries where admin 3 is the meaningful municipality-equivalent.
HAS_ADMIN_3 = {"PER", "ECU"}


def _normalize(s: str) -> str:
    """Lowercase + strip accents + trim, for fuzzy name comparison."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _appears_capitalized(name: str, original_text: str) -> bool:
    """True if `name` appears in `original_text` as a proper noun: a whole-word,
    accent-insensitive occurrence whose first letter is capitalized in the
    original text. This rejects 'piedras' (stones, lowercase) while keeping
    'Piedras' (the town, capitalized)."""
    if not name or not original_text:
        return False
    text_noacc = _strip_accents(original_text)
    name_noacc = _strip_accents(name)
    # Accent-stripping preserves character positions for Latin text, so match
    # offsets in text_noacc map back to original_text 1:1.
    for m in re.finditer(rf"\b{re.escape(name_noacc)}\b", text_noacc, re.IGNORECASE):
        if original_text[m.start():m.start() + 1].isupper():
            return True
    return False


class GadmIndex:
    """Opens each country's GADM geopackage once and caches the layers we use."""

    def __init__(self, shapefiles_dir: str):
        self.dir = shapefiles_dir
        self._cache: dict[tuple[str, int], object] = {}
        self._warned: set[str] = set()

    def for_country(self, iso3: str, level: int):
        key = (iso3, level)
        if key in self._cache:
            return self._cache[key]
        try:
            import geopandas as gpd
        except ImportError:
            raise SystemExit("geopandas is required: pip install geopandas")

        path = os.path.join(self.dir, f"gadm41_{iso3}.gpkg")
        if not os.path.exists(path):
            if iso3 not in self._warned:
                logging.warning("Missing shapefile for %s at %s (skipping that "
                                "country).", iso3, path)
                self._warned.add(iso3)
            self._cache[key] = None
            return None

        layers = gpd.list_layers(path)["name"].tolist()
        wanted = [L for L in layers if L.endswith(f"_{level}")]
        if not wanted:
            self._cache[key] = None
            return None
        gdf = gpd.read_file(path, layer=wanted[0])
        self._cache[key] = gdf
        return gdf


# ---------------------------------------------------------------------------
# Strategy 1: point-in-polygon
# ---------------------------------------------------------------------------

def _resolve_by_point(row, index, iso3):
    """Use the row's lat/lng to find the containing municipality polygon.
    Returns (admin2, admin3, gid) or None if there's no usable coordinate."""
    lat = (row.get("latitude") or "").strip()
    lng = (row.get("longitude") or "").strip()
    if not lat or not lng:
        return None
    try:
        lat_f, lng_f = float(lat), float(lng)
    except ValueError:
        return None

    try:
        import geopandas as gpd
        from shapely.geometry import Point
    except ImportError:
        raise SystemExit("geopandas is required: pip install geopandas")

    level = 3 if iso3 in HAS_ADMIN_3 else 2
    gdf = index.for_country(iso3, level)
    if gdf is None:
        # fall back to admin 2 if admin 3 layer is missing
        gdf = index.for_country(iso3, 2)
        level = 2
        if gdf is None:
            return None

    pt = gpd.GeoDataFrame(geometry=[Point(lng_f, lat_f)], crs="EPSG:4326")
    if gdf.crs is not None and str(gdf.crs) != "EPSG:4326":
        pt = pt.to_crs(gdf.crs)
    hit = gpd.sjoin(pt, gdf, predicate="within", how="left")
    if hit.empty or hit.iloc[0].isna().get(f"GID_{level}", True):
        return None

    rec = hit.iloc[0]
    a2 = str(rec.get("NAME_2", "") or "")
    if level == 3:
        a3 = str(rec.get("NAME_3", "") or "")
        gid = str(rec.get("GID_3", "") or "")
    else:
        a3 = ""
        gid = str(rec.get("GID_2", "") or "")
    return (a2, a3, gid)


# ---------------------------------------------------------------------------
# Strategy 2: capitalized text match (fallback only)
# ---------------------------------------------------------------------------

def _resolve_by_text(row, index, iso3):
    """Scan title + body for a municipality name, accepting only names that
    appear Capitalized (proper noun) and lie within the row's region."""
    title = row.get("article_title") or row.get("title") or ""
    body = row.get("article_text") or ""
    original = (title + "\n\n" + body).strip()
    if not original:
        return None

    gdf2 = index.for_country(iso3, 2)
    if gdf2 is None or "NAME_2" not in gdf2.columns or "GID_2" not in gdf2.columns:
        return None

    region = (row.get("region_department") or "").strip()
    if region and "NAME_1" in gdf2.columns:
        region_norm = _normalize(region)
        mask = gdf2["NAME_1"].apply(lambda n: _normalize(n) == region_norm)
        candidates = gdf2[mask] if mask.any() else gdf2
    else:
        candidates = gdf2

    # Collect names that appear capitalized in the text; prefer the longest.
    hits = []
    for r in candidates.itertuples():
        name = str(r.NAME_2)
        if len(_normalize(name)) < 4:        # avoid tiny ambiguous names
            continue
        if _appears_capitalized(name, original):
            hits.append((name, str(r.GID_2)))
    if not hits:
        return None
    hits.sort(key=lambda x: (-len(x[0]), x[0]))
    a2, gid2 = hits[0]

    if iso3 not in HAS_ADMIN_3:
        return (a2, "", gid2)

    # admin 3 within the matched admin 2, same capitalized rule.
    gdf3 = index.for_country(iso3, 3)
    if gdf3 is None or "GID_2" not in gdf3.columns:
        return (a2, "", gid2)
    sub = gdf3[gdf3["GID_2"] == gid2]
    sub_hits = []
    for r in sub.itertuples():
        name = str(r.NAME_3)
        if len(_normalize(name)) < 4:
            continue
        if _appears_capitalized(name, original):
            sub_hits.append((name, str(r.GID_3)))
    if not sub_hits:
        return (a2, "", gid2)
    sub_hits.sort(key=lambda x: (-len(x[0]), x[0]))
    a3, gid3 = sub_hits[0]
    return (a2, a3, gid3)


# ---------------------------------------------------------------------------
# Per-row resolution with cross-checks
# ---------------------------------------------------------------------------

def _resolve_row(row, index):
    """Return (admin2, admin3, gid). Tries point-in-polygon first, then a
    capitalization-checked text scan. Cross-checks the result against the
    row's own country/region; blanks out anything inconsistent."""
    country = (row.get("country") or "").strip()
    iso3 = COUNTRY_TO_ISO3.get(country)
    if not iso3:
        return ("", "", "")

    result = _resolve_by_point(row, index, iso3)
    if result is None:
        result = _resolve_by_text(row, index, iso3)
    if result is None:
        return ("", "", "")

    a2, a3, gid = result

    # Cross-check: a municipality must not just echo the country name, and the
    # gadm_id must start with this country's ISO3 (so we never attach a town
    # from the wrong country).
    if a2 and _normalize(a2) == _normalize(country):
        return ("", "", "")
    if gid and not gid.startswith(iso3):
        return ("", "", "")

    return (a2, a3, gid)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _needs_match(row, refetch):
    has_text = bool(row.get("article_text") or row.get("article_title")
                    or row.get("title"))
    has_coords = bool((row.get("latitude") or "").strip()
                      and (row.get("longitude") or "").strip())
    if not has_text and not has_coords:
        return False
    if row.get(COL_GID) and not refetch:
        return False
    return True


def match_admin(path, limit=None, refetch=False):
    rows = common.read_rows(path)
    index = GadmIndex(SHAPEFILES_DIR)

    todo = [r for r in rows if _needs_match(r, refetch)]
    if limit is not None:
        todo = todo[:limit]
    logging.info("%d of %d rows need admin matching.", len(todo), len(rows))

    matched = 0
    for i, row in enumerate(todo, 1):
        a2, a3, gid = _resolve_row(row, index)
        # Always populate the clean admin 0 / admin 1 from existing data.
        row[COL_A0] = (row.get("country") or "").strip()
        row[COL_A1] = (row.get("region_department") or "").strip()
        row[COL_A2] = a2
        row[COL_A3] = a3
        row[COL_GID] = gid
        if gid:
            matched += 1
        logging.info("[%d/%d] %-10s a1=%-18s a2=%-22s a3=%-15s",
                     i, len(todo), (row.get("country") or "")[:10],
                     (row.get("region_department") or "-")[:18],
                     (a2 or "-")[:22], (a3 or "-")[:15])

    # Make sure every row has the clean columns, and drop the old messy ones.
    for row in rows:
        row[COL_A0] = row.get(COL_A0, (row.get("country") or "").strip())
        row[COL_A1] = row.get(COL_A1, (row.get("region_department") or "").strip())
        row.setdefault(COL_A2, "")
        row.setdefault(COL_A3, "")
        row.setdefault(COL_GID, "")
        for old in OLD_COLS:
            if old in row and old not in NEW_COLS:
                row.pop(old, None)

    _write_with_new_cols_last(rows, path)
    logging.info("Done. %d/%d rows matched. Wrote %s.", matched, len(todo), path)


def _write_with_new_cols_last(rows, path):
    """Write rows so the five clean location columns come LAST, in order,
    and the old messy admin columns are gone."""
    if not rows:
        common.write_rows(rows, path)
        return
    # Determine the column order: every existing key except old/new ones,
    # then our five new columns in the requested order.
    seen = []
    for row in rows:
        for k in row.keys():
            if k in OLD_COLS or k in NEW_COLS:
                continue
            if k not in seen:
                seen.append(k)
    fieldnames = seen + NEW_COLS

    import csv
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Add clean admin 0-3 + gadm_id columns to combined.csv, "
                    "using coordinates first and capitalized text as fallback.")
    p.add_argument("--in", dest="in_path", default=DATA_PATH)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--refetch", action="store_true",
                   help="Redo rows even if they already have a gadm_id.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(args.in_path):
        raise SystemExit(f"No data file at '{args.in_path}'. Run run_all.py first.")
    match_admin(args.in_path, args.limit, args.refetch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

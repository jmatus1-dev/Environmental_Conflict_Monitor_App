"""
geocode.py - Step 3: turn place names into latitude/longitude.

Step 2 extracts a geocodable place string for each article (`location_context`,
e.g. "Puerto Inirida, Guainia, Colombia"). This script feeds that to a
geocoder and writes back `latitude` / `longitude` so the dashboard can map the
event precisely, not just at the country level.

Geocoder
--------
Uses `geopy` with OpenStreetMap's **Nominatim** backend: free, no API key. Its
usage policy caps you at ~1 request/second, so we wrap it in geopy's
`RateLimiter`. We also pass the country as a `countrycodes` filter to
disambiguate (e.g. there is a "San Martin" in several countries). For higher
accuracy/volume you can swap in a paid backend (Google, Mapbox, OpenCage) - the
`Geocoder` class is the only thing you'd change.

Caching
-------
Every lookup is cached on disk (`data/geocode_cache.json`) keyed by the query
string, so re-runs and repeated places cost nothing and don't re-hit the
service. Delete that file to force fresh lookups.

Precision
---------
We record `geocode_precision` (e.g. town / region / country) from what we were
able to resolve, and which query string produced the hit, so you can later
trust town-level points more than country-level fallbacks on the heatmap.

Setup
-----
    pip install geopy

Usage
-----
    python geocode.py                 # geocode data/combined.csv in place
    python geocode.py --limit 50      # only do 50 ungeocoded rows
    python geocode.py --no-country-fallback   # skip rows with no specific place
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import common

DEFAULT_IN = os.path.join("data", "combined.csv")
DEFAULT_CACHE = os.path.join("data", "geocode_cache.json")
NOMINATIM_USER_AGENT = "AImpactLab-EnvConflict/0.1 (research)"

# English country name -> ISO-3166 alpha-2, for Nominatim's countrycodes filter.
COUNTRY_TO_ISO2 = {
    "Colombia": "co", "Peru": "pe", "Ecuador": "ec", "Bolivia": "bo",
    "Brazil": "br", "Venezuela": "ve", "Argentina": "ar", "Chile": "cl",
    "Guatemala": "gt", "Mexico": "mx", "Paraguay": "py", "Uruguay": "uy",
    "Guyana": "gy", "Suriname": "sr", "French Guiana": "gf", "Panama": "pa",
    "Nicaragua": "ni", "Honduras": "hn", "Costa Rica": "cr",
}


# ---------------------------------------------------------------------------
# Geocoder (the only network-touching piece; injectable for testing)
# ---------------------------------------------------------------------------

class Geocoder:
    """Thin wrapper over geopy/Nominatim with a disk cache."""

    def __init__(self, cache_path: str = DEFAULT_CACHE, delay: float = 1.0):
        self.cache_path = cache_path
        self.cache: dict = self._load_cache()
        self._geocode = self._build_backend(delay)

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:  # noqa: BLE001 - corrupt cache shouldn't be fatal
                logging.warning("Could not read cache %s; starting fresh.",
                                self.cache_path)
        return {}

    def save_cache(self) -> None:
        common._ensure_parent(self.cache_path)
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0)
        os.replace(tmp, self.cache_path)

    def _build_backend(self, delay: float):
        """Return a rate-limited geocode callable. Imported lazily so the
        module imports without geopy installed (handy for tests)."""
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter
        nominatim = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=10)
        return RateLimiter(nominatim.geocode, min_delay_seconds=max(delay, 1.0),
                           max_retries=2, swallow_exceptions=True)

    def lookup(self, query: str, iso2: str | None) -> dict | None:
        """Geocode `query`, using the cache first. Returns a small result dict
        or None if nothing matched."""
        if not query:
            return None
        key = f"{query}|{iso2 or ''}"
        if key in self.cache:
            return self.cache[key]

        kwargs = {"addressdetails": True}
        if iso2:
            kwargs["country_codes"] = iso2
        loc = self._geocode(query, **kwargs)

        result = None
        if loc is not None:
            raw = getattr(loc, "raw", {}) or {}
            result = {
                "lat": loc.latitude,
                "lon": loc.longitude,
                "precision": _precision_of(raw),
                "display_name": raw.get("display_name", ""),
                "query": query,
            }
        self.cache[key] = result  # cache misses too, so we don't re-query them
        return result


def _precision_of(raw: dict) -> str:
    """Coarse precision label from a Nominatim result's address type/class."""
    addrtype = raw.get("addresstype") or raw.get("type") or ""
    if addrtype in ("city", "town", "village", "hamlet", "suburb",
                    "municipality", "locality"):
        return "town"
    if addrtype in ("state", "province", "region", "county", "administrative"):
        return "region"
    if addrtype in ("country",):
        return "country"
    if addrtype in ("river", "stream", "water", "natural", "protected_area",
                    "national_park"):
        return "feature"
    return addrtype or "unknown"


# ---------------------------------------------------------------------------
# Choosing what string to geocode for a row
# ---------------------------------------------------------------------------

def candidate_query(row: dict, country_fallback: bool) -> tuple[str, str]:
    """Pick the most specific geocodable string for a row, and a precision hint.
    Order: LLM location_context > location_name(+region+country) >
    region+country > country (only if country_fallback)."""
    country = (row.get("country") or "").strip()
    region = (row.get("region_department") or "").strip()
    ctx = (row.get("location_context") or "").strip()
    name = (row.get("location_name") or row.get("locality") or "").strip()

    if ctx:
        return ctx, "specific"
    if name:
        parts = [p for p in [name, region, country] if p]
        return ", ".join(parts), "specific"
    if region and country:
        return f"{region}, {country}", "region"
    if region:
        return region, "region"
    if country and country_fallback:
        return country, "country"
    return "", ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def needs_geocode(row: dict) -> bool:
    """A row needs geocoding if it has no usable latitude yet."""
    lat = (row.get("latitude") or "").strip()
    return lat == "" and (row.get("geocode_status") or "") != "no_location"


def geocode_dataset(in_path: str, geocoder: Geocoder, limit, country_fallback: bool) -> None:
    rows = common.read_rows(in_path)
    if not rows:
        raise SystemExit(f"No rows in {in_path}. Run run_all.py first.")

    todo = [r for r in rows if needs_geocode(r)]
    if limit is not None:
        todo = todo[:limit]
    logging.info("%d rows total, %d need geocoding%s.",
                 len(rows), len(todo),
                 f" (limited to {limit})" if limit is not None else "")
    if not todo:
        logging.info("Nothing to do.")
        return

    done = 0
    for i, row in enumerate(todo, 1):
        query, hint = candidate_query(row, country_fallback)
        if not query:
            row["geocode_status"] = "no_location"
            continue
        iso2 = COUNTRY_TO_ISO2.get((row.get("country") or "").strip())
        logging.info("[%d/%d] %s", i, len(todo), query[:70])
        res = geocoder.lookup(query, iso2)
        if res is None:
            row["geocode_status"] = "not_found"
            continue
        row["latitude"] = f"{res['lat']:.6f}"
        row["longitude"] = f"{res['lon']:.6f}"
        row["geocode_precision"] = res.get("precision", hint)
        row["geocode_query"] = res.get("query", query)
        row["geocode_display_name"] = res.get("display_name", "")
        row["geocode_status"] = "ok"
        done += 1

        if done % 25 == 0:
            geocoder.save_cache()
            common.write_rows(rows, in_path)
            logging.info("  checkpoint: %d geocoded so far.", done)

    geocoder.save_cache()
    common.write_rows(rows, in_path)
    logging.info("Geocoded %d rows; wrote %s.", done, in_path)


def parse_cli(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--in", dest="in_path", default=DEFAULT_IN,
                   help=f"Combined CSV to geocode in place (default: {DEFAULT_IN})")
    p.add_argument("--cache", default=DEFAULT_CACHE,
                   help=f"Geocode cache file (default: {DEFAULT_CACHE})")
    p.add_argument("--limit", type=int, default=None,
                   help="Geocode at most N ungeocoded rows.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Min seconds between geocoder calls (>=1.0 for Nominatim).")
    p.add_argument("--no-country-fallback", action="store_true",
                   help="Don't fall back to country-level coords when no "
                        "specific place is known (leave such rows off the map).")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_cli(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    geocoder = Geocoder(cache_path=args.cache, delay=args.delay)
    geocode_dataset(args.in_path, geocoder, args.limit,
                    country_fallback=not args.no_country_fallback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

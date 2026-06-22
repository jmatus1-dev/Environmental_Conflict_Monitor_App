"""
download_shapefiles.py - one-time download of GADM 4.1 shapefiles for South America.

Pulls one .gpkg file per country into ./shapefiles/. Each file contains every
administrative level for that country (country, state, municipality, ...).
Safe to re-run: files that are already on disk are skipped, and partial
downloads from prior failed runs are cleaned up.

Source: GADM 4.1 (https://gadm.org), free for academic / non-commercial use.

Run
---
    python3 download_shapefiles.py
"""

import os
import urllib.request

COUNTRIES = {
    "ARG": "Argentina",
    "BOL": "Bolivia",
    "BRA": "Brazil",
    "CHL": "Chile",
    "COL": "Colombia",
    "ECU": "Ecuador",
    "GUY": "Guyana",
    "PRY": "Paraguay",
    "PER": "Peru",
    "SUR": "Suriname",
    "URY": "Uruguay",
    "VEN": "Venezuela",
}

OUTPUT_DIR = "shapefiles"
BASE_URL = "https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg"
TIMEOUT = 180  # seconds; GADM's server is slow with big files like Brazil


def download(url, output_path):
    """Stream a file to disk in chunks, with a long timeout so big downloads
    don't trip on Mac's default 60-second socket timeout."""
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        with open(output_path, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for code, name in COUNTRIES.items():
        filename = f"gadm41_{code}.gpkg"
        output_path = os.path.join(OUTPUT_DIR, filename)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            print(f"already have {name}, skipping")
            continue

        url = f"{BASE_URL}/{filename}"
        print(f"downloading {name}...")
        try:
            download(url, output_path)
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            print(f"  done ({size_mb:.1f} MB)")
        except Exception as e:
            print(f"  failed: {e}")
            # Remove any partial file so the next run will retry cleanly.
            if os.path.exists(output_path):
                os.remove(output_path)

    print("\nDone. Files are in the 'shapefiles/' folder.")


if __name__ == "__main__":
    main()
